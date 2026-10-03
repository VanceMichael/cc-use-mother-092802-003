"""账本：哈希链、防篡改、离线补录标记、重启重放。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.grassland import GrasslandService, Ledger, LedgerError
from tests.helpers import build_scenario


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ops.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_entries_form_hash_chain(self) -> None:
        ledger = Ledger(self.path)
        e1 = ledger.append("plot.registered", {"plot_id": "P1"}, actor="a", event_date=date(2026, 9, 1))
        e2 = ledger.append("plot.registered", {"plot_id": "P2"}, actor="a", event_date=date(2026, 9, 2))
        self.assertEqual(e1.prev_hash, "0" * 64)
        self.assertEqual(e2.prev_hash, e1.hash)
        self.assertEqual([e.seq for e in ledger.read()], [1, 2])

    def test_tampered_line_is_rejected(self) -> None:
        ledger = Ledger(self.path)
        ledger.append("plot.registered", {"plot_id": "P1"}, actor="a", event_date=date(2026, 9, 1))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        lines[0] = lines[0].replace("P1", "PX")
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "摘要不符"):
            GrasslandService(Ledger(self.path))

    def test_backfill_is_marked_when_recorded_after_event(self) -> None:
        ledger = Ledger(self.path)
        entry = ledger.append(
            "forage.weighed", {"batch_id": "B1"},
            actor="m2", event_date=date(2026, 9, 10), recorded_date=date(2026, 10, 1),
        )
        self.assertTrue(entry.backfill)

    def test_future_event_date_rejected(self) -> None:
        with self.assertRaisesRegex(LedgerError, "业务日期不能晚于登记日期"):
            Ledger(self.path).append(
                "x", {}, actor="a", event_date=date(2026, 10, 2), recorded_date=date(2026, 10, 1)
            )

    def test_state_restores_after_restart_with_unfinished_work(self) -> None:
        svc = build_scenario(self.path)
        svc.open_task("T2", "P2", "2026-09-15", "2026-09-25", actor="admin", on="2026-09-14")
        svc.confirm_ecology("P2", "2026-09-08", "OBS-P2-0909", observer="obs1", on="2026-09-09")
        svc.schedule_job("J2", "T2", "M2", "C1", "2026-09-16", "2026-09-17", actor="admin")
        svc.start_job("J2", actor="foreman", on="2026-09-16")
        # 服务重启
        revived = GrasslandService(Ledger(self.path))
        pending = revived.unfinished()
        job_ids = {j["job_id"] for j in pending["jobs"]}
        task_ids = {t["task_id"] for t in pending["tasks"]}
        self.assertIn("J2", job_ids)
        self.assertIn("T2", task_ids)
        # 已完工的 J1/T1 不再出现在未完清单
        self.assertNotIn("J1", job_ids)
        self.assertNotIn("T1", task_ids)
        # 余额状态完整恢复
        batch = revived.state["batches"]["B1"]
        self.assertEqual(batch["out_sale_kg"], 8000)
        self.assertEqual(batch["out_subsidy_kg"], 1000)
        self.assertEqual(batch["stored_kg"] - 9000, 800)


if __name__ == "__main__":
    unittest.main()
