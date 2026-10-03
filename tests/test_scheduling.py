"""生态时序与调度：草籽成熟前置、休牧禁牧、地块/农机/班组互斥、故障延期改派。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.grassland import Conflict, GrasslandService, Ledger, RuleViolation


class EcologySchedulingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = GrasslandService(Ledger(Path(self.tmp.name) / "ops.jsonl"))
        s = self.svc
        s.register_plot("P1", "东湾", 12000.0, "边界A", actor="admin", on="2026-08-01")
        s.register_plot("P2", "西坡", 8000.0, "边界B", actor="admin", on="2026-08-01")
        s.register_machine("M1", "机一", actor="admin", on="2026-08-10")
        s.register_machine("M2", "机二", actor="admin", on="2026-08-10")
        s.register_crew("C1", "一组", ["m2"], actor="admin", on="2026-08-10")
        s.register_crew("C2", "二组", ["m3"], actor="admin", on="2026-08-10")
        s.confirm_ecology("P1", "2026-09-05", "OBS1", observer="obs1", on="2026-09-06")
        s.open_task("T1", "P1", "2026-09-07", "2026-09-20", actor="admin")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cannot_schedule_before_ecology_confirmed(self) -> None:
        self.svc.open_task("T2", "P2", "2026-09-07", "2026-09-20", actor="admin")
        with self.assertRaisesRegex(RuleViolation, "生态观察确认"):
            self.svc.schedule_job("Jx", "T2", "M2", "C2", "2026-09-08", "2026-09-09", actor="admin")

    def test_cannot_schedule_before_seed_maturity(self) -> None:
        with self.assertRaisesRegex(RuleViolation, "草籽成熟期"):
            self.svc.schedule_job("Jx", "T1", "M1", "C1", "2026-09-01", "2026-09-02", actor="admin")

    def test_restriction_period_blocks_scheduling(self) -> None:
        self.svc.impose_restriction(
            "R1", "P1", "禁牧", "2026-09-09", "2026-09-12",
            reason="生态管控", actor="admin", on="2026-09-01",
        )
        with self.assertRaisesRegex(RuleViolation, "禁牧期冲突"):
            self.svc.schedule_job("Jx", "T1", "M1", "C1", "2026-09-08", "2026-09-10", actor="admin")
        # 解除后可排
        self.svc.lift_restriction("R1", actor="admin", on="2026-09-02")
        self.svc.schedule_job("Jx", "T1", "M1", "C1", "2026-09-08", "2026-09-10", actor="admin")

    def test_only_one_effective_job_per_plot_per_timeslot(self) -> None:
        self.svc.schedule_job("J1", "T1", "M1", "C1", "2026-09-08", "2026-09-10", actor="admin")
        # 同一地块、另一机另一组、时间重叠 → 拒绝（须另开任务）
        self.svc.open_task("T1B", "P1", "2026-09-07", "2026-09-20", actor="admin", on="2026-09-07")
        with self.assertRaisesRegex(Conflict, "地块同一时段"):
            self.svc.schedule_job("J2", "T1B", "M2", "C2", "2026-09-09", "2026-09-11", actor="admin")
        # 同机跨地块重叠也拒绝
        self.svc.open_task("T2", "P2", "2026-09-07", "2026-09-25", actor="admin")
        self.svc.confirm_ecology("P2", "2026-09-05", "OBS2", observer="obs1", on="2026-09-06")
        with self.assertRaisesRegex(Conflict, "农机同一时段"):
            self.svc.schedule_job("J3", "T2", "M1", "C2", "2026-09-09", "2026-09-10", actor="admin")
        # 同组不同机不同地块同样拒绝
        with self.assertRaisesRegex(Conflict, "班组同一时段"):
            self.svc.schedule_job("J4", "T2", "M2", "C1", "2026-09-09", "2026-09-10", actor="admin")

    def test_non_overlapping_jobs_allowed_after_completion(self) -> None:
        self.svc.schedule_job("J1", "T1", "M1", "C1", "2026-09-08", "2026-09-10", actor="admin")
        self.svc.start_job("J1", actor="foreman", on="2026-09-08")
        self.svc.complete_job("J1", actor="foreman", on="2026-09-10")
        # 已完工作业不阻挡后续作业；同地块复打另开任务
        self.svc.open_task("T1B", "P1", "2026-09-11", "2026-09-13", actor="admin", on="2026-09-10")
        self.svc.schedule_job("J2", "T1B", "M1", "C1", "2026-09-11", "2026-09-12", actor="admin")

    def test_machine_breakdown_interrupt_repair_reschedule_flow(self) -> None:
        s = self.svc
        s.schedule_job("J1", "T1", "M1", "C1", "2026-09-08", "2026-09-10", actor="admin")
        s.start_job("J1", actor="foreman", on="2026-09-08")
        s.report_machine_down("M1", reason="割台故障", actor="driver", on="2026-09-09")
        s.interrupt_job("J1", reason="随农机故障中断", actor="foreman", on="2026-09-09")
        # 故障未修复不能复工
        with self.assertRaisesRegex(Conflict, "仍在故障"):
            s.resume_job("J1", actor="foreman", on="2026-09-10")
        s.repair_machine("M1", actor="repair", on="2026-09-11")
        # 超出任务窗口的延期被拒绝；延期任务窗口后才允许
        with self.assertRaisesRegex(RuleViolation, "超出任务窗口"):
            s.reschedule_job("J1", "2026-09-21", "2026-09-22", reason="越过窗口", actor="admin", on="2026-09-11")
        s.delay_task("T1", "2026-09-07", "2026-09-18", reason="配合故障顺延", actor="admin", on="2026-09-11")
        s.reschedule_job("J1", "2026-09-12", "2026-09-13", reason="故障修复后顺延", actor="admin", on="2026-09-11")
        # 也可改派到另一台机器和班组
        s.reschedule_job(
            "J1", "2026-09-14", "2026-09-15", machine_id="M2", crew_id="C2",
            reason="改派二组二号机", actor="admin", on="2026-09-13",
        )
        s.start_job("J1", actor="foreman", on="2026-09-14")
        s.complete_job("J1", actor="foreman", on="2026-09-15")
        self.assertEqual(s.state["jobs"]["J1"]["machine_id"], "M2")
        self.assertEqual(s.state["jobs"]["J1"]["crew_id"], "C2")
        self.assertEqual(s.state["tasks"]["T1"]["status"], "done")

    def test_down_machine_cannot_be_scheduled(self) -> None:
        self.svc.report_machine_down("M1", reason="保养", actor="driver", on="2026-09-01")
        with self.assertRaisesRegex(Conflict, "故障状态"):
            self.svc.schedule_job("Jx", "T1", "M1", "C1", "2026-09-08", "2026-09-09", actor="admin")


if __name__ == "__main__":
    unittest.main()
