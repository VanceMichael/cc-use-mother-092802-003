"""追加式哈希链账本：所有运营决定只增不改，重启后逐条重放恢复状态。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

from .errors import LedgerError

GENESIS = "0" * 64


def canonical(value: Any) -> str:
    """与键顺序、Unicode 表示稳定的规范化 JSON，用于哈希与摘要。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    seq: int
    event_type: str
    payload: dict[str, Any]
    actor: str
    event_date: date
    recorded_date: date
    backfill: bool
    prev_hash: str
    hash: str

    def to_line(self) -> str:
        body = {
            "seq": self.seq,
            "event_type": self.event_type,
            "payload": self.payload,
            "actor": self.actor,
            "event_date": self.event_date.isoformat(),
            "recorded_date": self.recorded_date.isoformat(),
            "backfill": self.backfill,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }
        return canonical(body)


def _parse_line(line: str) -> Entry:
    raw = json.loads(line)
    try:
        return Entry(
            seq=raw["seq"],
            event_type=raw["event_type"],
            payload=raw["payload"],
            actor=raw["actor"],
            event_date=date.fromisoformat(raw["event_date"]),
            recorded_date=date.fromisoformat(raw["recorded_date"]),
            backfill=raw["backfill"],
            prev_hash=raw["prev_hash"],
            hash=raw["hash"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerError("账本记录格式损坏") from exc


class Ledger:
    """JSONL 追加日志。写入即 fsync，读时校验序号连续与哈希链完整。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str,
        event_date: date,
        recorded_date: date | None = None,
    ) -> Entry:
        recorded_date = recorded_date or event_date
        if event_date > recorded_date:
            raise LedgerError("业务日期不能晚于登记日期")
        entries = self.read()
        seq = (entries[-1].seq + 1) if entries else 1
        prev_hash = entries[-1].hash if entries else GENESIS
        backfill = event_date < recorded_date
        entry = Entry(
            seq=seq,
            event_type=event_type,
            payload=payload,
            actor=actor,
            event_date=event_date,
            recorded_date=recorded_date,
            backfill=backfill,
            prev_hash=prev_hash,
            hash="",
        )
        body = {
            "seq": entry.seq,
            "event_type": entry.event_type,
            "payload": entry.payload,
            "actor": entry.actor,
            "event_date": entry.event_date.isoformat(),
            "recorded_date": entry.recorded_date.isoformat(),
            "backfill": entry.backfill,
            "prev_hash": entry.prev_hash,
        }
        object.__setattr__(entry, "hash", digest(body))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry.to_line() + "\n")
            handle.flush()
        return entry

    def read(self) -> list[Entry]:
        return list(self.iter_entries())

    def iter_entries(self) -> Iterator[Entry]:
        if not self.path.exists():
            return iter(())
        prev_hash = GENESIS
        expected_seq = 1
        with self.path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                entry = _parse_line(line)
                if entry.seq != expected_seq:
                    raise LedgerError(f"第{line_no}行序号断裂")
                if entry.prev_hash != prev_hash:
                    raise LedgerError(f"第{line_no}行哈希链断裂")
                body = {
                    "seq": entry.seq,
                    "event_type": entry.event_type,
                    "payload": entry.payload,
                    "actor": entry.actor,
                    "event_date": entry.event_date.isoformat(),
                    "recorded_date": entry.recorded_date.isoformat(),
                    "backfill": entry.backfill,
                    "prev_hash": entry.prev_hash,
                }
                if digest(body) != entry.hash:
                    raise LedgerError(f"第{line_no}行内容与摘要不符")
                yield entry
                prev_hash = entry.hash
                expected_seq += 1

    def replay(self, projector: "StateProjector[Any]") -> Any:
        for entry in self.iter_entries():
            projector.apply(entry)
        return projector.state


class StateProjector:
    """按事件类型分派的只读状态投影基类，重放时复用同一套规则。"""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.handlers: dict[str, Any] = {}

    def apply(self, entry: Entry) -> None:
        handler = self.handlers.get(entry.event_type)
        if handler is not None:
            handler(entry.payload, entry)
