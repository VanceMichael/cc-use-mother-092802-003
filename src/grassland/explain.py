"""把一笔分红、优惠牧草或务工收入还原为牧民看得懂的证据链。

证据按账本序号排列，每一条都能指回具体的地块、作业、批次或决定，
并标注登记人、业务日期和是否离线补录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import NotFound
from .ledger import Entry
from .service import GrasslandService


@dataclass(frozen=True)
class Evidence:
    seq: int
    title: str
    detail: str
    actor: str
    event_date: str
    backfill: bool

    def line(self) -> str:
        flag = "（离线补录）" if self.backfill else ""
        return f"[{self.seq}] {self.event_date} {self.title}：{self.detail}（登记人：{self.actor}）{flag}"


@dataclass
class Explanation:
    kind: str
    subject: str
    summary: str
    evidence: list[Evidence] = field(default_factory=list)

    def render_text(self) -> str:
        body = "\n".join(item.line() for item in self.evidence)
        return f"【{self.kind}】{self.subject}\n结论：{self.summary}\n依据：\n{body}"


def _entry_evidence(
    entry: Entry, title: str, detail: str
) -> Evidence:
    return Evidence(
        seq=entry.seq,
        title=title,
        detail=detail,
        actor=entry.actor,
        event_date=entry.event_date.isoformat(),
        backfill=entry.backfill,
    )


def _find_entries(service: GrasslandService, event_type: str, **match: Any) -> list[Entry]:
    result = []
    for entry in service.entries:
        if entry.event_type != event_type:
            continue
        if all(entry.payload.get(key) == value for key, value in match.items()):
            result.append(entry)
    return result


def explain_dividend(service: GrasslandService, plan_id: str, member_id: str) -> Explanation:
    """分红依据：成员当期股份版本、年度销售回款、已确认支出、方案草案/纠错/发布、调整单。"""
    plan = service.state["plans"].get(plan_id)
    if plan is None:
        raise NotFound(f"分配方案不存在：{plan_id}")
    line = next((item for item in plan["lines"] if item["member_id"] == member_id), None)
    if line is None:
        raise NotFound("成员不在方案名单中")
    member = service.state["members"][member_id]
    settlement = service.member_settlement(plan_id, member_id)

    evidence: list[Evidence] = []

    # 股份从哪里来：只引用账本中真实发生、且在基准日已生效的股份事件。
    registered = _find_entries(service, "member.registered", member_id=member_id)
    for entry in registered:
        version = entry.payload["versions"][0]
        if version["effective_date"] <= plan["as_of"]:
            evidence.append(
                _entry_evidence(
                    entry,
                    "入社股份登记",
                    f"自 {version['effective_date']} 起持有 {version['shares']} 股（{version['reason']}）",
                )
            )
    for entry in (
        _find_entries(service, "member.share_changed", member_id=member_id)
        + _find_entries(service, "member.rejoined", member_id=member_id)
    ):
        if entry.payload["effective_date"] <= plan["as_of"]:
            evidence.append(
                _entry_evidence(
                    entry,
                    "股份变更",
                    f"自 {entry.payload['effective_date']} 起持有 {entry.payload['shares']} 股（{entry.payload['reason']}）",
                )
            )
    for entry in _find_entries(service, "member.left", member_id=member_id):
        if entry.payload["effective_date"] <= plan["as_of"]:
            evidence.append(_entry_evidence(entry, "成员退出", entry.payload["reason"]))

    # 收入从哪里来：逐笔销售追到批次、任务、地块。
    year = plan["year"]
    for entry in _find_entries(service, "forage.dispatched", kind="sale"):
        if entry.event_date.year != year:
            continue
        p = entry.payload
        batch = service.state["batches"][p["batch_id"]]
        evidence.append(
            _entry_evidence(
                entry,
                "对外销售",
                f"批次 {p['batch_id']}（地块 {batch['plot_id']}，任务 {batch['task_id']}）"
                f"售出 {p['quantity_kg']} 千克，单价 {p['unit_price']} 元，回款 {p['amount']} 元，购方：{p['buyer']}",
            )
        )

    # 扣减了哪些已确认支出。
    for entry in _find_entries(service, "expense.confirmed"):
        expense = service.state["expenses"][entry.payload["expense_id"]]
        if expense["date"][:4] != str(year):
            continue
        evidence.append(
            Evidence(
                entry.seq,
                f"已确认{expense['kind']}",
                f"{expense['detail']}，支出 {expense['amount']} 元（录入：{expense['recorder']}，确认：{expense['confirmer']}）",
                entry.actor,
                entry.event_date.isoformat(),
                entry.backfill,
            )
        )
    for entry in _find_entries(service, "wage.confirmed"):
        wage = service.state["wages"][entry.payload["wage_id"]]
        if wage["date"][:4] != str(year):
            continue
        worker = service.state["members"][wage["member_id"]]["name"]
        evidence.append(
            Evidence(
                entry.seq,
                "已确认务工报酬",
                f"成员 {worker} 作业 {wage['job_id']}，{wage['work_days']} 个工天，{wage['amount']} 元",
                entry.actor,
                entry.event_date.isoformat(),
                entry.backfill,
            )
        )

    # 方案本身的形成过程。
    for entry in _find_entries(service, "plan.drafted", plan_id=plan_id):
        evidence.append(_entry_evidence(entry, "方案草案", f"{year} 年度净收益 {plan['net']} 元，合计 {plan['total_shares']} 股"))
    for entry in _find_entries(service, "plan.corrected", plan_id=plan_id):
        evidence.append(_entry_evidence(entry, "发布前纠错", entry.payload["reason"]))
    for entry in _find_entries(service, "plan.published", plan_id=plan_id):
        evidence.append(_entry_evidence(entry, "方案发布", f"发布人：{entry.actor}"))
    for entry in _find_entries(service, "plan.adjustment", plan_id=plan_id, member_id=member_id):
        p = entry.payload
        evidence.append(_entry_evidence(entry, f"发布后{p['kind']}", f"{p['kind']} {p['amount']} 元，原因：{p['reason']}"))

    evidence.sort(key=lambda item: (item.seq, item.event_date))
    summary = (
        f"{member['name']} 当期 {line['shares']} 股，占 {plan['total_shares']} 股；"
        f"{year} 年收入 {plan['revenue']} 元、已确认支出 {plan['expenses']} 元、净收益 {plan['net']} 元，"
        f"应分 {settlement['base_amount']} 元"
    )
    if settlement["adjustments"]:
        summary += f"，经补发/追缴调整后实得 {settlement['dividend']} 元"
    return Explanation("分红", f"{plan_id} / {member_id}", summary, evidence)


def explain_subsidy(service: GrasslandService, dispatch_id: str) -> Explanation:
    """优惠牧草依据：出库单 → 批次称重/损耗/入库 → 任务地块 → 可选分配方案。"""
    dispatch = service.state["dispatches"].get(dispatch_id)
    if dispatch is None or dispatch["kind"] != "subsidy":
        raise NotFound(f"优惠供应单不存在：{dispatch_id}")
    batch = service.state["batches"][dispatch["batch_id"]]
    member = service.state["members"][dispatch["member_id"]]
    evidence: list[Evidence] = []

    weigh = _find_entries(service, "forage.weighed", batch_id=batch["batch_id"])[0]
    evidence.append(
        _entry_evidence(
            weigh,
            "批次称重",
            f"毛重 {batch['gross_kg']} 千克、皮重 {batch['tare_kg']} 千克、净重 {batch['net_kg']} 千克",
        )
    )
    for entry in _find_entries(service, "forage.loss_recorded", batch_id=batch["batch_id"]):
        evidence.append(_entry_evidence(entry, "损耗登记", f"{entry.payload['reason']}，累计损耗 {entry.payload['total_loss_kg']} 千克"))
    stored = _find_entries(service, "forage.stored", batch_id=batch["batch_id"])[0]
    evidence.append(
        _entry_evidence(
            stored,
            "入库确认",
            f"入库 {batch['stored_kg']} 千克至 {batch['storehouse']}（净重 − 损耗，数量一致）",
        )
    )
    out_entry = _find_entries(service, "forage.dispatched", dispatch_id=dispatch_id)[0]
    evidence.append(
        _entry_evidence(
            out_entry,
            "优惠供应出库",
            f"供应给 {member['name']} {dispatch['quantity_kg']} 千克，优惠价 {dispatch['price_per_kg']} 元/千克",
        )
    )
    if dispatch.get("plan_id"):
        for entry in _find_entries(service, "plan.published", plan_id=dispatch["plan_id"]):
            evidence.append(_entry_evidence(entry, "分配方案", f"优惠供应依据方案 {dispatch['plan_id']}"))

    evidence.sort(key=lambda item: item.seq)
    remaining = batch["stored_kg"] - batch["out_subsidy_kg"] - batch["out_sale_kg"]
    plot = service.state["plots"][batch["plot_id"]]
    summary = (
        f"{member['name']} 领取的 {dispatch['quantity_kg']} 千克牧草来自批次 {batch['batch_id']}"
        f"（地块 {plot['name']}，任务 {batch['task_id']}）；该批入库 {batch['stored_kg']} 千克，"
        f"累计出库 {batch['out_subsidy_kg'] + batch['out_sale_kg']} 千克，结存 {remaining} 千克，数量守恒。"
    )
    return Explanation("优惠牧草", dispatch_id, summary, evidence)


def explain_wage(service: GrasslandService, wage_id: str) -> Explanation:
    """务工收入依据：务工单双岗确认 → 作业全生命周期 → 任务窗口/生态前置 → 农机班组。"""
    wage = service.state["wages"].get(wage_id)
    if wage is None:
        raise NotFound(f"务工单不存在：{wage_id}")
    job = service.state["jobs"][wage["job_id"]]
    task = service.state["tasks"][job["task_id"]]
    plot = service.state["plots"][job["plot_id"]]
    evidence: list[Evidence] = []

    readiness = service.state["readiness"].get(job["plot_id"])
    if readiness:
        eco = _find_entries(
            service, "ecology.observation_confirmed", plot_id=job["plot_id"]
        )[-1]
        evidence.append(
            _entry_evidence(
                eco,
                "生态前置确认",
                f"草籽成熟日 {readiness['seed_mature_date']}，观察记录 {readiness['observation_id']}",
            )
        )
    for entry in _find_entries(service, "harvest_task.opened", task_id=task["task_id"]):
        evidence.append(
            _entry_evidence(entry, "收割任务", f"地块 {plot['name']}，窗口 {task['window_start']} 至 {task['window_end']}")
        )
    for entry in _find_entries(service, "harvest_task.delayed", task_id=task["task_id"]):
        evidence.append(_entry_evidence(entry, "任务延期", entry.payload["reason"]))
    lifecycle = (
        ("job.scheduled", "排产", lambda p: f"农机 {p['machine_id']}、班组 {p['crew_id']}，{p['start']} 至 {p['end']}"),
        ("job.started", "开工", lambda p: "作业开始"),
        ("job.interrupted", "作业中断", lambda p: p["reason"]),
        ("job.rescheduled", "延期/改派", lambda p: f"{p['reason']}；改为农机 {p['machine_id']}、班组 {p['crew_id']}，{p['start']} 至 {p['end']}"),
        ("job.completed", "完工", lambda p: "作业完成"),
    )
    for event_type, title, render in lifecycle:
        for entry in _find_entries(service, event_type, job_id=job["job_id"]):
            evidence.append(_entry_evidence(entry, title, render(entry.payload)))
    for entry in _find_entries(service, "machine.broken_down", machine_id=job["machine_id"]):
        evidence.append(_entry_evidence(entry, "农机故障", entry.payload["reason"]))
    for entry in _find_entries(service, "machine.repaired", machine_id=job["machine_id"]):
        evidence.append(_entry_evidence(entry, "农机修复", "恢复可用"))

    recorded = _find_entries(service, "wage.recorded", wage_id=wage_id)[0]
    evidence.append(
        _entry_evidence(recorded, "务工报酬登记", f"{wage['work_days']} 个工天，{wage['amount']} 元")
    )
    confirmed = _find_entries(service, "wage.confirmed", wage_id=wage_id)
    if confirmed:
        evidence.append(
            _entry_evidence(
                confirmed[0],
                "务工报酬确认",
                f"由 {wage['confirmer']} 独立确认（录入人：{wage['recorder']}，非同一人）",
            )
        )
    else:
        evidence.append(
            Evidence(0, "待确认", f"务工单已登记但未经第二人确认，暂不计入分配支出", "", wage["date"], False)
        )

    evidence.sort(key=lambda item: item.seq)
    member = service.state["members"][wage["member_id"]]
    status = "已确认计入" if wage["confirmed"] else "尚待第二人确认"
    summary = (
        f"{member['name']} 在作业 {job['job_id']}（地块 {plot['name']}，班组 {job['crew_id']}，"
        f"农机 {job['machine_id']}）出工 {wage['work_days']} 天，应得 {wage['amount']} 元，{status}。"
    )
    return Explanation("务工收入", wage_id, summary, evidence)
