"""集体草场运营服务：在追加式账本之上实现全部业务规则。

所有写操作只追加事件；服务启动或重启时重放整条哈希链重建状态，
因此未完任务、离线补录与成员资格变化都能完整恢复。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from .errors import Conflict, LedgerError, NotFound, RuleViolation
from .ledger import Entry, Ledger

# 金额统一为两位小数字符串，重量统一为整数千克，避免浮点误差。
Z2 = Decimal("0.01")


def money(value: Decimal | int | str) -> Decimal:
    amount = Decimal(value)
    if amount != amount.quantize(Z2):
        raise RuleViolation("金额必须精确到分")
    return amount


def _d(value: str) -> date:
    return date.fromisoformat(value)


def _overlap(start_a: date, end_a: date, start_b: date, end_b: date) -> bool:
    """半开区间 [start, end) 相交判断。"""
    return start_a < end_b and start_b < end_a


class GrasslandService:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.entries: list[Entry] = []
        self.state: dict[str, Any] = {}
        self._rebuild()

    # ------------------------------------------------------------------ 重建

    def _rebuild(self) -> None:
        self.entries = []
        self.state = {
            "plots": {},
            "restrictions": {},
            "readiness": {},
            "machines": {},
            "crews": {},
            "members": {},
            "tasks": {},
            "jobs": {},
            "batches": {},
            "dispatches": {},
            "expenses": {},
            "wages": {},
            "plans": {},
        }
        dispatch = {
            "plot.registered": self._h_plot_registered,
            "plot.retired": self._h_plot_retired,
            "restriction.imposed": self._h_restriction_imposed,
            "restriction.lifted": self._h_restriction_lifted,
            "ecology.observation_confirmed": self._h_readiness,
            "machine.registered": self._h_machine_registered,
            "machine.broken_down": self._h_machine_down,
            "machine.repaired": self._h_machine_repaired,
            "crew.registered": self._h_crew_registered,
            "crew.disbanded": self._h_crew_disbanded,
            "member.registered": self._h_member_registered,
            "member.share_changed": self._h_member_share,
            "member.left": self._h_member_left,
            "member.rejoined": self._h_member_rejoined,
            "harvest_task.opened": self._h_task_opened,
            "harvest_task.delayed": self._h_task_delayed,
            "job.scheduled": self._h_job_scheduled,
            "job.started": self._h_job_started,
            "job.interrupted": self._h_job_interrupted,
            "job.rescheduled": self._h_job_rescheduled,
            "job.completed": self._h_job_completed,
            "job.cancelled": self._h_job_cancelled,
            "forage.weighed": self._h_weighed,
            "forage.loss_recorded": self._h_loss,
            "forage.stored": self._h_stored,
            "forage.dispatched": self._h_dispatched,
            "expense.recorded": self._h_expense_recorded,
            "expense.confirmed": self._h_expense_confirmed,
            "wage.recorded": self._h_wage_recorded,
            "wage.confirmed": self._h_wage_confirmed,
            "plan.drafted": self._h_plan_drafted,
            "plan.corrected": self._h_plan_corrected,
            "plan.published": self._h_plan_published,
            "plan.adjustment": self._h_plan_adjustment,
        }
        for entry in self.ledger.iter_entries():
            handler = dispatch.get(entry.event_type)
            if handler is None:
                raise LedgerError(f"未知事件类型：{entry.event_type}")
            handler(entry.payload, entry)
            self.entries.append(entry)

    def _append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str,
        event_date: date | str,
        recorded_date: date | str | None = None,
    ) -> Entry:
        if isinstance(event_date, str):
            event_date = _d(event_date)
        if isinstance(recorded_date, str):
            recorded_date = _d(recorded_date)
        entry = self.ledger.append(
            event_type,
            payload,
            actor=actor,
            event_date=event_date,
            recorded_date=recorded_date,
        )
        self._rebuild()
        return entry

    # ---------------------------------------------------------- 地块与生态约束

    def register_plot(
        self, plot_id: str, name: str, area_mu: float, boundary: str, *, actor: str, on: date | str
    ) -> Entry:
        if plot_id in self.state["plots"]:
            raise Conflict(f"地块已存在：{plot_id}")
        if area_mu <= 0:
            raise RuleViolation("地块面积必须为正")
        return self._append(
            "plot.registered",
            {"plot_id": plot_id, "name": name, "area_mu": area_mu, "boundary": boundary},
            actor=actor,
            event_date=on,
        )

    def retire_plot(self, plot_id: str, *, reason: str, actor: str, on: date | str) -> Entry:
        self._require_plot(plot_id)
        return self._append(
            "plot.retired",
            {"plot_id": plot_id, "reason": reason},
            actor=actor,
            event_date=on,
        )

    def impose_restriction(
        self,
        restriction_id: str,
        plot_id: str,
        kind: str,
        start: date | str,
        end: date | str,
        *,
        reason: str,
        actor: str,
        on: date | str,
    ) -> Entry:
        self._require_plot(plot_id)
        if kind not in ("休牧", "禁牧"):
            raise RuleViolation("管制类型只能是休牧或禁牧")
        start_d, end_d = _d(str(start)), _d(str(end))
        if start_d >= end_d:
            raise RuleViolation("管制结束日期必须晚于开始日期")
        if restriction_id in self.state["restrictions"]:
            raise Conflict(f"管制记录已存在：{restriction_id}")
        return self._append(
            "restriction.imposed",
            {
                "restriction_id": restriction_id,
                "plot_id": plot_id,
                "kind": kind,
                "start": start_d.isoformat(),
                "end": end_d.isoformat(),
                "reason": reason,
                "lifted": False,
            },
            actor=actor,
            event_date=on,
        )

    def lift_restriction(self, restriction_id: str, *, actor: str, on: date | str) -> Entry:
        if restriction_id not in self.state["restrictions"]:
            raise NotFound(f"管制记录不存在：{restriction_id}")
        return self._append(
            "restriction.lifted", {"restriction_id": restriction_id}, actor=actor, event_date=on
        )

    def confirm_ecology(
        self,
        plot_id: str,
        seed_mature_on: date | str,
        observation_id: str,
        *,
        observer: str,
        on: date | str,
        note: str = "",
    ) -> Entry:
        """生态监测人员确认草籽成熟并完成生态观察，是排产的前置条件。"""
        self._require_plot(plot_id)
        mature = _d(str(seed_mature_on))
        if _d(str(on)) < mature:
            raise RuleViolation("草籽尚未成熟，不能出具成熟确认")
        prior = self.state["readiness"].get(plot_id)
        if prior and prior["seed_mature_date"] <= mature.isoformat():
            raise Conflict("该地块已有同等或更早的生态确认")
        return self._append(
            "ecology.observation_confirmed",
            {
                "plot_id": plot_id,
                "seed_mature_date": mature.isoformat(),
                "observation_id": observation_id,
                "observer": observer,
                "note": note,
            },
            actor=observer,
            event_date=on,
        )

    # -------------------------------------------------------------- 农机与班组

    def register_machine(self, machine_id: str, name: str, *, actor: str, on: date | str) -> Entry:
        if machine_id in self.state["machines"]:
            raise Conflict(f"农机已存在：{machine_id}")
        return self._append(
            "machine.registered",
            {"machine_id": machine_id, "name": name, "status": "available"},
            actor=actor,
            event_date=on,
        )

    def report_machine_down(self, machine_id: str, *, reason: str, actor: str, on: date | str) -> Entry:
        machine = self._require_machine(machine_id)
        if machine["status"] == "down":
            raise Conflict("农机已处于故障状态")
        return self._append(
            "machine.broken_down",
            {"machine_id": machine_id, "reason": reason},
            actor=actor,
            event_date=on,
        )

    def repair_machine(self, machine_id: str, *, actor: str, on: date | str) -> Entry:
        machine = self._require_machine(machine_id)
        if machine["status"] != "down":
            raise Conflict("农机并未故障")
        return self._append(
            "machine.repaired", {"machine_id": machine_id}, actor=actor, event_date=on
        )

    def register_crew(self, crew_id: str, name: str, members: list[str], *, actor: str, on: date | str) -> Entry:
        if crew_id in self.state["crews"]:
            raise Conflict(f"班组已存在：{crew_id}")
        if not members:
            raise RuleViolation("班组至少需要一名成员")
        return self._append(
            "crew.registered",
            {"crew_id": crew_id, "name": name, "members": members, "status": "active"},
            actor=actor,
            event_date=on,
        )

    def disband_crew(self, crew_id: str, *, actor: str, on: date | str) -> Entry:
        if self._require_crew(crew_id)["status"] != "active":
            raise Conflict("班组已解散")
        return self._append(
            "crew.disbanded", {"crew_id": crew_id}, actor=actor, event_date=on
        )

    # --------------------------------------------------------------- 成员与股份

    def register_member(
        self,
        member_id: str,
        name: str,
        shares: int,
        effective_on: date | str,
        *,
        actor: str,
        on: date | str | None = None,
    ) -> Entry:
        if member_id in self.state["members"]:
            raise Conflict(f"成员已存在：{member_id}")
        if shares < 0:
            raise RuleViolation("股份不能为负")
        effective = _d(str(effective_on))
        return self._append(
            "member.registered",
            {
                "member_id": member_id,
                "name": name,
                "versions": [
                    {"effective_date": effective.isoformat(), "shares": shares, "reason": "入社登记"}
                ],
                "status": "active",
            },
            actor=actor,
            event_date=on or effective,
        )

    def change_shares(
        self,
        member_id: str,
        shares: int,
        effective_on: date | str,
        *,
        reason: str,
        actor: str,
        on: date | str | None = None,
    ) -> Entry:
        member = self._require_member(member_id)
        if shares < 0:
            raise RuleViolation("股份不能为负")
        effective = _d(str(effective_on))
        if effective <= _d(member["versions"][-1]["effective_date"]):
            raise RuleViolation("股份新版本生效日期必须晚于上一版本")
        return self._append(
            "member.share_changed",
            {
                "member_id": member_id,
                "effective_date": effective.isoformat(),
                "shares": shares,
                "reason": reason,
            },
            actor=actor,
            event_date=on or effective,
        )

    def member_left(self, member_id: str, effective_on: date | str, *, reason: str, actor: str) -> Entry:
        member = self._require_member(member_id)
        if member["status"] != "active":
            raise Conflict("成员当前不是在册状态")
        return self._append(
            "member.left",
            {"member_id": member_id, "effective_date": str(effective_on), "reason": reason},
            actor=actor,
            event_date=effective_on,
        )

    def member_rejoined(
        self, member_id: str, shares: int, effective_on: date | str, *, reason: str, actor: str
    ) -> Entry:
        member = self._require_member(member_id)
        if member["status"] == "active":
            raise Conflict("成员仍在册")
        return self._append(
            "member.rejoined",
            {
                "member_id": member_id,
                "effective_date": str(effective_on),
                "shares": shares,
                "reason": reason,
            },
            actor=actor,
            event_date=effective_on,
        )

    def effective_shares(self, member_id: str, as_of: date | str) -> int | None:
        """返回成员在某日的有效股份；尚未入社或已退出期间为 0，未知成员为 None。"""
        member = self.state["members"].get(member_id)
        if member is None:
            return None
        as_of_d = _d(str(as_of))
        shares = None
        for version in member["versions"]:
            if _d(version["effective_date"]) <= as_of_d:
                shares = version["shares"]
        return shares

    # ------------------------------------------------------------------ 收割调度

    def open_task(
        self,
        task_id: str,
        plot_id: str,
        window_start: date | str,
        window_end: date | str,
        *,
        actor: str,
        on: date | str | None = None,
    ) -> Entry:
        self._require_plot(plot_id)
        start_d, end_d = _d(str(window_start)), _d(str(window_end))
        if start_d >= end_d:
            raise RuleViolation("作业窗口结束日期必须晚于开始日期")
        if task_id in self.state["tasks"]:
            raise Conflict(f"收割任务已存在：{task_id}")
        return self._append(
            "harvest_task.opened",
            {
                "task_id": task_id,
                "plot_id": plot_id,
                "window_start": start_d.isoformat(),
                "window_end": end_d.isoformat(),
                "status": "pending",
            },
            actor=actor,
            event_date=on or start_d,
        )

    def delay_task(
        self,
        task_id: str,
        new_start: date | str,
        new_end: date | str,
        *,
        reason: str,
        actor: str,
        on: date | str,
    ) -> Entry:
        task = self._require_task(task_id)
        if task["status"] == "done":
            raise Conflict("已完成任务不能延期")
        start_d, end_d = _d(str(new_start)), _d(str(new_end))
        if start_d >= end_d:
            raise RuleViolation("作业窗口结束日期必须晚于开始日期")
        return self._append(
            "harvest_task.delayed",
            {
                "task_id": task_id,
                "window_start": start_d.isoformat(),
                "window_end": end_d.isoformat(),
                "reason": reason,
            },
            actor=actor,
            event_date=on,
        )

    def schedule_job(
        self,
        job_id: str,
        task_id: str,
        machine_id: str,
        crew_id: str,
        start: date | str,
        end: date | str,
        *,
        actor: str,
        on: date | str | None = None,
        recorded_date: date | str | None = None,
    ) -> Entry:
        task = self._require_task(task_id)
        if task["status"] != "pending":
            raise Conflict("只有待安排任务可以排产")
        plot_id = task["plot_id"]
        self._check_plot_open(plot_id)
        start_d, end_d = _d(str(start)), _d(str(end))
        if start_d >= end_d:
            raise RuleViolation("作业结束日期必须晚于开始日期")
        # 调度先满足草籽成熟与生态观察，再校验管制期与任务窗口
        self._check_ecology(plot_id, start_d)
        self._check_restriction(plot_id, start_d, end_d)
        if not (
            _d(task["window_start"]) <= start_d
            and end_d <= _d(task["window_end"])
        ):
            raise RuleViolation("作业时间超出任务窗口")
        machine = self._require_machine(machine_id)
        crew = self._require_crew(crew_id)
        if machine["status"] != "available":
            raise Conflict("农机处于故障状态，不能排产")
        if crew["status"] != "active":
            raise Conflict("班组已解散，不能排产")
        self._check_resource_free(plot_id, machine_id, crew_id, start_d, end_d)
        return self._append(
            "job.scheduled",
            {
                "job_id": job_id,
                "task_id": task_id,
                "plot_id": plot_id,
                "machine_id": machine_id,
                "crew_id": crew_id,
                "start": start_d.isoformat(),
                "end": end_d.isoformat(),
                "status": "scheduled",
            },
            actor=actor,
            event_date=on or start_d,
            recorded_date=recorded_date,
        )

    def start_job(self, job_id: str, *, actor: str, on: date | str) -> Entry:
        job = self._require_job(job_id)
        if job["status"] not in ("scheduled", "interrupted"):
            raise Conflict("只有已排产或中断过的作业可以开工")
        return self._append(
            "job.started", {"job_id": job_id}, actor=actor, event_date=on
        )

    def interrupt_job(self, job_id: str, *, reason: str, actor: str, on: date | str) -> Entry:
        """作业中断（如农机故障、天气）。若因故障中断，农机须另报故障。"""
        job = self._require_job(job_id)
        if job["status"] != "in_progress":
            raise Conflict("只有进行中的作业可以中断")
        return self._append(
            "job.interrupted", {"job_id": job_id, "reason": reason}, actor=actor, event_date=on
        )

    def resume_job(self, job_id: str, *, actor: str, on: date | str) -> Entry:
        """复工：中断后的作业重新进入进行中（例如故障修复、天气转好）。"""
        job = self._require_job(job_id)
        if job["status"] != "interrupted":
            raise Conflict("只有中断的作业可以复工")
        if self.state["machines"][job["machine_id"]]["status"] != "available":
            raise Conflict("作业所用农机仍在故障，不能复工")
        return self._append(
            "job.started", {"job_id": job_id, "resumed": True}, actor=actor, event_date=on
        )

    def reschedule_job(
        self,
        job_id: str,
        start: date | str,
        end: date | str,
        *,
        machine_id: str | None = None,
        crew_id: str | None = None,
        reason: str,
        actor: str,
        on: date | str,
    ) -> Entry:
        """作业延期或改派：原作业保留痕迹，重新校验地块/机器/班组互斥。"""
        job = self._require_job(job_id)
        if job["status"] not in ("scheduled", "interrupted"):
            raise Conflict("当前作业状态不能改期")
        task = self._require_task(job["task_id"])
        start_d, end_d = _d(str(start)), _d(str(end))
        if start_d >= end_d:
            raise RuleViolation("作业结束日期必须晚于开始日期")
        machine_id = machine_id or job["machine_id"]
        crew_id = crew_id or job["crew_id"]
        self._check_ecology(job["plot_id"], start_d)
        self._check_restriction(job["plot_id"], start_d, end_d)
        if not (_d(task["window_start"]) <= start_d and end_d <= _d(task["window_end"])):
            raise RuleViolation("作业时间超出任务窗口；如需改期请先延期任务")
        if self._require_machine(machine_id)["status"] != "available":
            raise Conflict("改派农机处于故障状态")
        if self._require_crew(crew_id)["status"] != "active":
            raise Conflict("改派班组已解散")
        self._check_resource_free(
            job["plot_id"], machine_id, crew_id, start_d, end_d, exclude_job=job_id
        )
        return self._append(
            "job.rescheduled",
            {
                "job_id": job_id,
                "machine_id": machine_id,
                "crew_id": crew_id,
                "start": start_d.isoformat(),
                "end": end_d.isoformat(),
                "reason": reason,
            },
            actor=actor,
            event_date=on,
        )

    def complete_job(self, job_id: str, *, actor: str, on: date | str) -> Entry:
        job = self._require_job(job_id)
        if job["status"] != "in_progress":
            raise Conflict("作业未在进行中，不能完工（中断后须先复工）")
        return self._append(
            "job.completed", {"job_id": job_id}, actor=actor, event_date=on
        )

    def cancel_job(self, job_id: str, *, reason: str, actor: str, on: date | str) -> Entry:
        job = self._require_job(job_id)
        if job["status"] in ("completed", "cancelled"):
            raise Conflict("作业已终结，不能取消")
        return self._append(
            "job.cancelled", {"job_id": job_id, "reason": reason}, actor=actor, event_date=on
        )

    def unfinished(self) -> dict[str, list[dict[str, Any]]]:
        """重启恢复入口：列出仍需跟进的任务与作业。"""
        return {
            "tasks": [t for t in self.state["tasks"].values() if t["status"] != "done"],
            "jobs": [
                j
                for j in self.state["jobs"].values()
                if j["status"] in ("scheduled", "in_progress", "interrupted")
            ],
        }

    # -------------------------------------------------------------- 牧草批次守恒

    def weigh_forage(
        self,
        batch_id: str,
        task_id: str,
        gross_kg: int,
        tare_kg: int,
        *,
        weigher: str,
        on: date | str,
        recorded_date: date | str | None = None,
    ) -> Entry:
        """称重登记：净重 = 毛重 − 皮重，为批次守恒的起点。支持离线补录。"""
        task = self._require_task(task_id)
        if batch_id in self.state["batches"]:
            raise Conflict(f"批次已存在：{batch_id}")
        if gross_kg <= 0 or tare_kg < 0 or gross_kg <= tare_kg:
            raise RuleViolation("称重数据无效：毛重必须为正且大于皮重")
        net = gross_kg - tare_kg
        return self._append(
            "forage.weighed",
            {
                "batch_id": batch_id,
                "task_id": task_id,
                "plot_id": task["plot_id"],
                "gross_kg": gross_kg,
                "tare_kg": tare_kg,
                "net_kg": net,
                "loss_kg": 0,
                "stored_kg": 0,
                "out_subsidy_kg": 0,
                "out_sale_kg": 0,
                "stored": False,
                "weigher": weigher,
            },
            actor=weigher,
            event_date=on,
            recorded_date=recorded_date,
        )

    def record_loss(self, batch_id: str, loss_kg: int, *, reason: str, actor: str, on: date | str) -> Entry:
        batch = self._require_batch(batch_id)
        if batch["stored"]:
            raise Conflict("批次已入库，损耗应在入库前记录")
        if loss_kg <= 0:
            raise RuleViolation("损耗必须为正数")
        new_loss = batch["loss_kg"] + loss_kg
        if new_loss >= batch["net_kg"]:
            raise RuleViolation("累计损耗不能达到或超过净重")
        return self._append(
            "forage.loss_recorded",
            {"batch_id": batch_id, "loss_kg": loss_kg, "total_loss_kg": new_loss, "reason": reason},
            actor=actor,
            event_date=on,
        )

    def store_forage(
        self, batch_id: str, storehouse: str, *, actor: str, on: date | str
    ) -> Entry:
        """入库确认：入库量必须严格等于净重减累计损耗。"""
        batch = self._require_batch(batch_id)
        if batch["stored"]:
            raise Conflict("批次已入库，不能重复入库")
        stored_kg = batch["net_kg"] - batch["loss_kg"]
        return self._append(
            "forage.stored",
            {"batch_id": batch_id, "storehouse": storehouse, "stored_kg": stored_kg},
            actor=actor,
            event_date=on,
        )

    def dispatch_subsidy(
        self,
        dispatch_id: str,
        batch_id: str,
        member_id: str,
        quantity_kg: int,
        price_per_kg: Decimal | str,
        *,
        plan_id: str | None = None,
        actor: str,
        on: date | str,
    ) -> Entry:
        member = self._require_member(member_id)
        if plan_id is not None:
            self._require_plan(plan_id)
        price = money(price_per_kg)
        amount = money(quantity_kg) * price
        return self._dispatch(
            dispatch_id,
            batch_id,
            quantity_kg,
            kind="subsidy",
            actor=actor,
            on=on,
            extra={
                "member_id": member_id,
                "price_per_kg": str(price),
                "market_amount": str(amount),
                "plan_id": plan_id,
            },
        )

    def sell_forage(
        self,
        sale_id: str,
        batch_id: str,
        quantity_kg: int,
        unit_price: Decimal | str,
        *,
        buyer: str,
        actor: str,
        on: date | str,
    ) -> Entry:
        amount = money(quantity_kg) * money(unit_price)
        return self._dispatch(
            sale_id,
            batch_id,
            quantity_kg,
            kind="sale",
            actor=actor,
            on=on,
            extra={"buyer": buyer, "unit_price": str(money(unit_price)), "amount": str(amount)},
        )

    def _dispatch(
        self,
        dispatch_id: str,
        batch_id: str,
        quantity_kg: int,
        *,
        kind: str,
        actor: str,
        on: date | str,
        extra: dict[str, Any],
    ) -> Entry:
        batch = self._require_batch(batch_id)
        if not batch["stored"]:
            raise RuleViolation("批次尚未入库，不能出库")
        if quantity_kg <= 0:
            raise RuleViolation("出库数量必须为正")
        remaining = (
            batch["stored_kg"] - batch["out_subsidy_kg"] - batch["out_sale_kg"]
        )
        if quantity_kg > remaining:
            raise RuleViolation(
                f"出库超出批次结存：申请{quantity_kg}千克，结存{remaining}千克"
            )
        if dispatch_id in self.state["dispatches"]:
            raise Conflict(f"出库单已存在：{dispatch_id}")
        payload = {
            "dispatch_id": dispatch_id,
            "batch_id": batch_id,
            "kind": kind,
            "quantity_kg": quantity_kg,
        }
        payload.update(extra)
        return self._append(
            "forage.dispatched", payload, actor=actor, event_date=on
        )

    # ---------------------------------------------------------- 成本、公益、务工

    def record_expense(
        self,
        expense_id: str,
        kind: str,
        amount: Decimal | str,
        *,
        detail: str,
        actor: str,
        on: date | str,
    ) -> Entry:
        """登记支出（销售成本/作业成本/公益支出）。须经另一人确认才计入净收益。"""
        if kind not in ("销售成本", "作业成本", "公益支出"):
            raise RuleViolation("支出类别不被支持")
        if expense_id in self.state["expenses"]:
            raise Conflict(f"支出单已存在：{expense_id}")
        amount = money(amount)
        if amount <= 0:
            raise RuleViolation("支出金额必须为正")
        return self._append(
            "expense.recorded",
            {
                "expense_id": expense_id,
                "kind": kind,
                "amount": str(amount),
                "detail": detail,
                "recorder": actor,
                "confirmed": False,
            },
            actor=actor,
            event_date=on,
        )

    def confirm_expense(self, expense_id: str, *, actor: str, on: date | str) -> Entry:
        expense = self.state["expenses"].get(expense_id)
        if expense is None:
            raise NotFound(f"支出单不存在：{expense_id}")
        if expense["confirmed"]:
            raise Conflict("支出已确认")
        if expense["recorder"] == actor:
            raise RuleViolation("录入人与确认人不能是同一人")
        return self._append(
            "expense.confirmed", {"expense_id": expense_id}, actor=actor, event_date=on
        )

    def record_wage(
        self,
        wage_id: str,
        member_id: str,
        job_id: str,
        amount: Decimal | str,
        *,
        work_days: float,
        actor: str,
        on: date | str,
    ) -> Entry:
        self._require_member(member_id)
        job = self._require_job(job_id)
        if job["status"] != "completed":
            raise RuleViolation("务工报酬应在作业完工后登记")
        amount = money(amount)
        if amount <= 0 or work_days <= 0:
            raise RuleViolation("务工报酬与工天必须为正")
        if wage_id in self.state["wages"]:
            raise Conflict(f"务工单已存在：{wage_id}")
        return self._append(
            "wage.recorded",
            {
                "wage_id": wage_id,
                "member_id": member_id,
                "job_id": job_id,
                "amount": str(amount),
                "work_days": work_days,
                "recorder": actor,
                "confirmed": False,
            },
            actor=actor,
            event_date=on,
        )

    def confirm_wage(self, wage_id: str, *, actor: str, on: date | str) -> Entry:
        wage = self.state["wages"].get(wage_id)
        if wage is None:
            raise NotFound(f"务工单不存在：{wage_id}")
        if wage["confirmed"]:
            raise Conflict("务工单已确认")
        if wage["recorder"] == actor:
            raise RuleViolation("录入人与确认人不能是同一人")
        return self._append(
            "wage.confirmed", {"wage_id": wage_id}, actor=actor, event_date=on
        )

    # ------------------------------------------------------------- 年度分配方案

    def annual_financials(self, year: int) -> dict[str, Any]:
        revenue = Decimal("0.00")
        expenses = Decimal("0.00")
        unconfirmed = []
        for dispatch in self.state["dispatches"].values():
            if dispatch["kind"] == "sale" and _d(dispatch["date"]).year == year:
                revenue += Decimal(dispatch["amount"])
        for expense in self.state["expenses"].values():
            if _d(expense["date"]).year != year:
                continue
            if expense["confirmed"]:
                expenses += Decimal(expense["amount"])
            else:
                unconfirmed.append(expense["expense_id"])
        for wage in self.state["wages"].values():
            if _d(wage["date"]).year == year and wage["confirmed"]:
                expenses += Decimal(wage["amount"])
        return {
            "year": year,
            "revenue": revenue.quantize(Z2),
            "expenses": expenses.quantize(Z2),
            "net": (revenue - expenses).quantize(Z2),
            "unconfirmed_expenses": unconfirmed,
        }

    def draft_plan(
        self, plan_id: str, year: int, *, as_of: date | str, actor: str, on: date | str
    ) -> Entry:
        if plan_id in self.state["plans"]:
            raise Conflict(f"分配方案已存在：{plan_id}")
        snapshot = self._compute_plan_snapshot(year, as_of, share_overrides={})
        return self._append(
            "plan.drafted",
            {"plan_id": plan_id, "year": year, "as_of": str(as_of), **snapshot},
            actor=actor,
            event_date=on,
        )

    def correct_plan(
        self,
        plan_id: str,
        *,
        share_overrides: dict[str, int] | None = None,
        reason: str,
        actor: str,
        on: date | str,
    ) -> Entry:
        """发布前纠错：例如依据成员资格变化重新认定当期股份，并重算每股金额。"""
        plan = self._require_plan(plan_id)
        if plan["status"] != "draft":
            raise RuleViolation("方案已发布，只能通过补发或追缴调整")
        overrides = dict(plan["share_overrides"])
        for member_id, shares in (share_overrides or {}).items():
            self._require_member(member_id)
            if shares < 0:
                raise RuleViolation("股份不能为负")
            overrides[member_id] = shares
        snapshot = self._compute_plan_snapshot(plan["year"], plan["as_of"], overrides)
        return self._append(
            "plan.corrected",
            {
                "plan_id": plan_id,
                "share_overrides": overrides,
                "reason": reason,
                **snapshot,
            },
            actor=actor,
            event_date=on,
        )

    def publish_plan(self, plan_id: str, *, actor: str, on: date | str) -> Entry:
        """发布分配方案。负责录入产量的成员不能独自确认含本人分红的方案。

        产量录入人取本年度称重事件的登记人（成员编号）。
        """
        plan = self._require_plan(plan_id)
        if plan["status"] != "draft":
            raise RuleViolation("方案不是草案状态")
        plan_member_ids = {line["member_id"] for line in plan["lines"]}
        year = plan["year"]
        weighers = {
            entry.actor
            for entry in self.entries
            if entry.event_type == "forage.weighed" and entry.event_date.year == year
        }
        if actor in weighers and actor in plan_member_ids:
            raise RuleViolation(
                "产量录入人不能独自确认含本人结算的方案，请由其他财务人员发布"
            )
        return self._append(
            "plan.published", {"plan_id": plan_id}, actor=actor, event_date=on
        )

    def adjust_plan(
        self,
        adjustment_id: str,
        plan_id: str,
        member_id: str,
        kind: str,
        amount: Decimal | str,
        *,
        reason: str,
        actor: str,
        on: date | str,
    ) -> Entry:
        """发布后调整：补发（补付成员）或追缴（向成员追回），原方案保持不变。"""
        plan = self._require_plan(plan_id)
        if plan["status"] != "published":
            raise RuleViolation("方案尚未发布，应直接纠错草案")
        if kind not in ("补发", "追缴"):
            raise RuleViolation("调整类型只能是补发或追缴")
        amount = money(amount)
        if amount <= 0:
            raise RuleViolation("调整金额必须为正")
        if not any(line["member_id"] == member_id for line in plan["lines"]):
            raise NotFound("该成员不在方案名单中")
        if adjustment_id in {a["adjustment_id"] for a in plan["adjustments"]}:
            raise Conflict("调整单已存在")
        return self._append(
            "plan.adjustment",
            {
                "adjustment_id": adjustment_id,
                "plan_id": plan_id,
                "member_id": member_id,
                "kind": kind,
                "amount": str(amount),
                "reason": reason,
            },
            actor=actor,
            event_date=on,
        )

    def member_settlement(self, plan_id: str, member_id: str) -> dict[str, Any]:
        plan = self._require_plan(plan_id)
        line = next((line for line in plan["lines"] if line["member_id"] == member_id), None)
        if line is None:
            raise NotFound("成员不在方案名单中")
        total = Decimal(line["amount"])
        for adj in plan["adjustments"]:
            if adj["member_id"] != member_id:
                continue
            total += Decimal(adj["amount"]) if adj["kind"] == "补发" else -Decimal(adj["amount"])
        return {
            "plan_id": plan_id,
            "year": plan["year"],
            "member_id": member_id,
            "dividend": str(total.quantize(Z2)),
            "base_amount": line["amount"],
            "adjustments": [a for a in plan["adjustments"] if a["member_id"] == member_id],
        }

    def _compute_plan_snapshot(
        self, year: int, as_of: date | str, share_overrides: dict[str, int]
    ) -> dict[str, Any]:
        financials = self.annual_financials(year)
        lines: list[dict[str, Any]] = []
        total_shares = 0
        for member_id, member in self.state["members"].items():
            shares = share_overrides.get(member_id)
            if shares is None:
                shares = self.effective_shares(member_id, as_of)
            if shares is None or shares <= 0:
                continue
            lines.append({"member_id": member_id, "name": member["name"], "shares": shares})
            total_shares += shares
        net = financials["net"]
        if net < 0:
            raise RuleViolation("年度净收益为负，不能形成分红方案")
        allocated = Decimal("0.00")
        for index, line in enumerate(lines):
            if index == len(lines) - 1:
                amount = net - allocated  # 尾差给最后一户，保证分文不差
            else:
                amount = (net * line["shares"] / total_shares).quantize(Z2)
                allocated += amount
            line["amount"] = str(amount)
        return {
            "as_of": str(as_of),
            "revenue": str(financials["revenue"]),
            "expenses": str(financials["expenses"]),
            "net": str(net),
            "total_shares": total_shares,
            "lines": lines,
            "unconfirmed_expenses": financials["unconfirmed_expenses"],
            "share_overrides": dict(share_overrides),
        }

    # ------------------------------------------------------------------ 校验辅助

    def _require_plot(self, plot_id: str) -> dict[str, Any]:
        plot = self.state["plots"].get(plot_id)
        if plot is None:
            raise NotFound(f"地块不存在：{plot_id}")
        if plot["status"] != "active":
            raise Conflict("地块已退出经营")
        return plot

    def _require_machine(self, machine_id: str) -> dict[str, Any]:
        machine = self.state["machines"].get(machine_id)
        if machine is None:
            raise NotFound(f"农机不存在：{machine_id}")
        return machine

    def _require_crew(self, crew_id: str) -> dict[str, Any]:
        crew = self.state["crews"].get(crew_id)
        if crew is None:
            raise NotFound(f"班组不存在：{crew_id}")
        return crew

    def _require_member(self, member_id: str) -> dict[str, Any]:
        member = self.state["members"].get(member_id)
        if member is None:
            raise NotFound(f"成员不存在：{member_id}")
        return member

    def _require_task(self, task_id: str) -> dict[str, Any]:
        task = self.state["tasks"].get(task_id)
        if task is None:
            raise NotFound(f"收割任务不存在：{task_id}")
        return task

    def _require_job(self, job_id: str) -> dict[str, Any]:
        job = self.state["jobs"].get(job_id)
        if job is None:
            raise NotFound(f"作业不存在：{job_id}")
        return job

    def _require_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.state["batches"].get(batch_id)
        if batch is None:
            raise NotFound(f"牧草批次不存在：{batch_id}")
        return batch

    def _require_plan(self, plan_id: str) -> dict[str, Any]:
        plan = self.state["plans"].get(plan_id)
        if plan is None:
            raise NotFound(f"分配方案不存在：{plan_id}")
        return plan

    def _check_plot_open(self, plot_id: str) -> None:
        self._require_plot(plot_id)

    def _check_ecology(self, plot_id: str, start_d: date) -> None:
        ready = self.state["readiness"].get(plot_id)
        if ready is None:
            raise RuleViolation("地块尚未完成草籽成熟与生态观察确认，不能排产")
        if start_d < _d(ready["seed_mature_date"]):
            raise RuleViolation("作业开始时间早于草籽成熟期，不符合错峰收割要求")

    def _check_restriction(self, plot_id: str, start_d: date, end_d: date) -> None:
        for restriction in self.state["restrictions"].values():
            if restriction["plot_id"] != plot_id or restriction["lifted"]:
                continue
            if _overlap(start_d, end_d, _d(restriction["start"]), _d(restriction["end"])):
                raise RuleViolation(
                    f"作业时间与{restriction['kind']}期冲突（{restriction['restriction_id']}）"
                )

    def _check_resource_free(
        self,
        plot_id: str,
        machine_id: str,
        crew_id: str,
        start_d: date,
        end_d: date,
        *,
        exclude_job: str | None = None,
    ) -> None:
        for job in self.state["jobs"].values():
            if job["status"] in ("completed", "cancelled") or job["job_id"] == exclude_job:
                continue
            if not _overlap(start_d, end_d, _d(job["start"]), _d(job["end"])):
                continue
            if job["plot_id"] == plot_id:
                raise Conflict(f"地块同一时段已有有效作业：{job['job_id']}")
            if job["machine_id"] == machine_id:
                raise Conflict(f"农机同一时段已被占用：{job['job_id']}")
            if job["crew_id"] == crew_id:
                raise Conflict(f"班组同一时段已有作业：{job['job_id']}")

    # ------------------------------------------------------------------ 事件处理

    def _h_plot_registered(self, p: dict[str, Any], e: Entry) -> None:
        self.state["plots"][p["plot_id"]] = {
            **p,
            "status": "active",
            "seq": e.seq,
            "date": e.event_date.isoformat(),
        }

    def _h_plot_retired(self, p: dict[str, Any], e: Entry) -> None:
        self.state["plots"][p["plot_id"]]["status"] = "retired"

    def _h_restriction_imposed(self, p: dict[str, Any], e: Entry) -> None:
        self.state["restrictions"][p["restriction_id"]] = {**p, "seq": e.seq}

    def _h_restriction_lifted(self, p: dict[str, Any], e: Entry) -> None:
        self.state["restrictions"][p["restriction_id"]]["lifted"] = True

    def _h_readiness(self, p: dict[str, Any], e: Entry) -> None:
        self.state["readiness"][p["plot_id"]] = {**p, "seq": e.seq}

    def _h_machine_registered(self, p: dict[str, Any], e: Entry) -> None:
        self.state["machines"][p["machine_id"]] = {**p, "seq": e.seq}

    def _h_machine_down(self, p: dict[str, Any], e: Entry) -> None:
        self.state["machines"][p["machine_id"]]["status"] = "down"

    def _h_machine_repaired(self, p: dict[str, Any], e: Entry) -> None:
        self.state["machines"][p["machine_id"]]["status"] = "available"

    def _h_crew_registered(self, p: dict[str, Any], e: Entry) -> None:
        self.state["crews"][p["crew_id"]] = {**p, "seq": e.seq}

    def _h_crew_disbanded(self, p: dict[str, Any], e: Entry) -> None:
        self.state["crews"][p["crew_id"]]["status"] = "disbanded"

    def _h_member_registered(self, p: dict[str, Any], e: Entry) -> None:
        self.state["members"][p["member_id"]] = {
            **p,
            "status_since": p["versions"][0]["effective_date"],
            "seq": e.seq,
        }

    def _h_member_share(self, p: dict[str, Any], e: Entry) -> None:
        member = self.state["members"][p["member_id"]]
        member["versions"].append(
            {"effective_date": p["effective_date"], "shares": p["shares"], "reason": p["reason"]}
        )

    def _h_member_left(self, p: dict[str, Any], e: Entry) -> None:
        member = self.state["members"][p["member_id"]]
        member["status"] = "left"
        member["status_since"] = p["effective_date"]
        # 退出当期起股份归零，使历史日期的权益计算自动剔除失格成员。
        member["versions"].append(
            {"effective_date": p["effective_date"], "shares": 0, "reason": p["reason"]}
        )

    def _h_member_rejoined(self, p: dict[str, Any], e: Entry) -> None:
        member = self.state["members"][p["member_id"]]
        member["status"] = "active"
        member["status_since"] = p["effective_date"]
        member["versions"].append(
            {"effective_date": p["effective_date"], "shares": p["shares"], "reason": p["reason"]}
        )

    def _h_task_opened(self, p: dict[str, Any], e: Entry) -> None:
        self.state["tasks"][p["task_id"]] = {**p, "seq": e.seq}

    def _h_task_delayed(self, p: dict[str, Any], e: Entry) -> None:
        task = self.state["tasks"][p["task_id"]]
        task["window_start"] = p["window_start"]
        task["window_end"] = p["window_end"]
        task["delay_reason"] = p["reason"]

    def _h_job_scheduled(self, p: dict[str, Any], e: Entry) -> None:
        self.state["jobs"][p["job_id"]] = {**p, "seq": e.seq}
        self.state["tasks"][p["task_id"]]["status"] = "scheduled"

    def _h_job_started(self, p: dict[str, Any], e: Entry) -> None:
        job = self.state["jobs"][p["job_id"]]
        job["status"] = "in_progress"
        if p.get("resumed"):
            job.setdefault("resumptions", []).append(e.seq)
        self.state["tasks"][job["task_id"]]["status"] = "active"

    def _h_job_interrupted(self, p: dict[str, Any], e: Entry) -> None:
        self.state["jobs"][p["job_id"]]["status"] = "interrupted"

    def _h_job_rescheduled(self, p: dict[str, Any], e: Entry) -> None:
        job = self.state["jobs"][p["job_id"]]
        job["machine_id"] = p["machine_id"]
        job["crew_id"] = p["crew_id"]
        job["start"] = p["start"]
        job["end"] = p["end"]
        job["status"] = "scheduled"
        job["reschedule_reason"] = p["reason"]

    def _h_job_completed(self, p: dict[str, Any], e: Entry) -> None:
        job = self.state["jobs"][p["job_id"]]
        job["status"] = "completed"
        self.state["tasks"][job["task_id"]]["status"] = "done"

    def _h_job_cancelled(self, p: dict[str, Any], e: Entry) -> None:
        job = self.state["jobs"][p["job_id"]]
        job["status"] = "cancelled"
        self.state["tasks"][job["task_id"]]["status"] = "pending"

    def _h_weighed(self, p: dict[str, Any], e: Entry) -> None:
        self.state["batches"][p["batch_id"]] = {**p, "date": e.event_date.isoformat(), "seq": e.seq}

    def _h_loss(self, p: dict[str, Any], e: Entry) -> None:
        batch = self.state["batches"][p["batch_id"]]
        batch["loss_kg"] = p["total_loss_kg"]

    def _h_stored(self, p: dict[str, Any], e: Entry) -> None:
        batch = self.state["batches"][p["batch_id"]]
        batch["stored"] = True
        batch["stored_kg"] = p["stored_kg"]
        batch["storehouse"] = p["storehouse"]

    def _h_dispatched(self, p: dict[str, Any], e: Entry) -> None:
        batch = self.state["batches"][p["batch_id"]]
        if p["kind"] == "subsidy":
            batch["out_subsidy_kg"] += p["quantity_kg"]
        else:
            batch["out_sale_kg"] += p["quantity_kg"]
        self.state["dispatches"][p["dispatch_id"]] = {
            **p,
            "date": e.event_date.isoformat(),
            "seq": e.seq,
        }

    def _h_expense_recorded(self, p: dict[str, Any], e: Entry) -> None:
        self.state["expenses"][p["expense_id"]] = {
            **p,
            "date": e.event_date.isoformat(),
            "seq": e.seq,
        }

    def _h_expense_confirmed(self, p: dict[str, Any], e: Entry) -> None:
        self.state["expenses"][p["expense_id"]]["confirmed"] = True
        self.state["expenses"][p["expense_id"]]["confirmer"] = e.actor

    def _h_wage_recorded(self, p: dict[str, Any], e: Entry) -> None:
        self.state["wages"][p["wage_id"]] = {
            **p,
            "date": e.event_date.isoformat(),
            "seq": e.seq,
        }

    def _h_wage_confirmed(self, p: dict[str, Any], e: Entry) -> None:
        self.state["wages"][p["wage_id"]]["confirmed"] = True
        self.state["wages"][p["wage_id"]]["confirmer"] = e.actor

    def _h_plan_drafted(self, p: dict[str, Any], e: Entry) -> None:
        self.state["plans"][p["plan_id"]] = {
            **p,
            "status": "draft",
            "adjustments": [],
            "seq": e.seq,
        }

    def _h_plan_corrected(self, p: dict[str, Any], e: Entry) -> None:
        plan = self.state["plans"][p["plan_id"]]
        for key in ("revenue", "expenses", "net", "total_shares", "lines", "share_overrides",
                    "unconfirmed_expenses"):
            plan[key] = p[key]
        plan.setdefault("corrections", []).append(
            {"reason": p["reason"], "seq": e.seq, "actor": e.actor}
        )

    def _h_plan_published(self, p: dict[str, Any], e: Entry) -> None:
        plan = self.state["plans"][p["plan_id"]]
        plan["status"] = "published"
        plan["published_seq"] = e.seq
        plan["publisher"] = e.actor

    def _h_plan_adjustment(self, p: dict[str, Any], e: Entry) -> None:
        self.state["plans"][p["plan_id"]]["adjustments"].append({**p, "seq": e.seq})
