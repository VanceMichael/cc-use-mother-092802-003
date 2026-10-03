"""成员股份版本与年度分配：资格变化、草案纠错、发布分离、补发追缴。"""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests.helpers import build_scenario

from src.grassland import Conflict, GrasslandService, Ledger, NotFound, RuleViolation


class MembershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = GrasslandService(Ledger(Path(self.tmp.name) / "ops.jsonl"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_share_versions_take_effect_on_dates(self) -> None:
        s = self.svc
        s.register_member("m1", "阿荣", 10, "2026-01-01", actor="admin")
        self.assertEqual(s.effective_shares("m1", "2026-06-01"), 10)
        s.change_shares("m1", 12, "2026-07-01", reason="新增人口配股", actor="admin")
        self.assertEqual(s.effective_shares("m1", "2026-06-30"), 10)
        self.assertEqual(s.effective_shares("m1", "2026-07-01"), 12)
        with self.assertRaisesRegex(RuleViolation, "必须晚于上一版本"):
            s.change_shares("m1", 13, "2026-06-01", reason="倒签", actor="admin")

    def test_member_leaving_and_rejoining(self) -> None:
        s = self.svc
        s.register_member("m9", "新来户", 5, "2026-01-01", actor="admin")
        s.member_left("m9", "2026-08-01", reason="户口迁出", actor="admin")
        self.assertEqual(s.effective_shares("m9", "2026-08-01"), 0)
        self.assertEqual(s.effective_shares("m9", "2026-12-31"), 0)
        with self.assertRaisesRegex(Conflict, "不是在册"):
            s.member_left("m9", "2026-09-01", reason="重复退出", actor="admin")
        s.member_rejoined("m9", 5, "2026-10-01", reason="回迁恢复资格", actor="admin")
        self.assertEqual(s.effective_shares("m9", "2026-10-01"), 5)


class DistributionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ops.jsonl"
        self.svc = build_scenario(self.path)
        # 场景：销售收入 6400；已确认支出 1200 + 务工 500 → 净收益 4700

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_financials_only_count_confirmed_items(self) -> None:
        s = self.svc
        s.record_expense("E3", "公益支出", "300.00", detail="未公示的慰问支出", actor="staff1", on="2026-12-01")
        fin = s.annual_financials(2026)
        self.assertEqual(fin["revenue"], Decimal("6400.00"))
        self.assertEqual(fin["expenses"], Decimal("1700.00"))
        self.assertEqual(fin["net"], Decimal("4700.00"))
        self.assertIn("E3", fin["unconfirmed_expenses"])

    def test_plan_allocation_matches_net_and_shares(self) -> None:
        s = self.svc
        s.draft_plan("PLAN-2026", 2026, as_of="2026-12-31", actor="fin1", on="2027-01-05")
        plan = s.state["plans"]["PLAN-2026"]
        self.assertEqual(plan["total_shares"], 20)
        amounts = {line["member_id"]: Decimal(line["amount"]) for line in plan["lines"]}
        # 10/20=2350，6/20=1410，尾差户得 940，合计严格等于净收益
        self.assertEqual(amounts["m1"], Decimal("2350.00"))
        self.assertEqual(amounts["m2"], Decimal("1410.00"))
        self.assertEqual(sum(amounts.values(), Decimal("0.00")), Decimal("4700.00"))

    def test_correction_allowed_only_before_publication(self) -> None:
        s = self.svc
        # m3 退出后草案将其剔除，发布前纠错恢复其配股
        s.member_left("m3", "2026-12-01", reason="资格待核暂停分配", actor="admin")
        s.draft_plan("PLAN-2026", 2026, as_of="2026-12-31", actor="fin1", on="2027-01-05")
        self.assertEqual(s.state["plans"]["PLAN-2026"]["total_shares"], 16)
        s.correct_plan("PLAN-2026", share_overrides={"m3": 4}, reason="复核资格恢复配股", actor="fin1", on="2027-01-06")
        self.assertEqual(s.state["plans"]["PLAN-2026"]["total_shares"], 20)
        s.publish_plan("PLAN-2026", actor="fin2", on="2027-01-08")
        with self.assertRaisesRegex(RuleViolation, "只能通过补发或追缴"):
            s.correct_plan("PLAN-2026", share_overrides={"m3": 3}, reason="发布后改数", actor="fin1", on="2027-01-09")

    def test_weigher_cannot_publish_plan_containing_own_settlement(self) -> None:
        s = self.svc
        s.draft_plan("PLAN-2026", 2026, as_of="2026-12-31", actor="fin1", on="2027-01-05")
        # m2 是本年度称重人，且本人在分红名单中
        with self.assertRaisesRegex(RuleViolation, "不能独自确认含本人结算"):
            s.publish_plan("PLAN-2026", actor="m2", on="2027-01-08")
        # 另一名财务人员可发布
        s.publish_plan("PLAN-2026", actor="fin2", on="2027-01-08")

    def test_published_plan_changes_only_by_adjustment(self) -> None:
        s = self.svc
        s.draft_plan("PLAN-2026", 2026, as_of="2026-12-31", actor="fin1", on="2027-01-05")
        s.publish_plan("PLAN-2026", actor="fin2", on="2027-01-08")
        s.adjust_plan("A1", "PLAN-2026", "m1", "补发", "120.00", reason="漏算上半年配股", actor="fin2", on="2027-01-10")
        s.adjust_plan("A2", "PLAN-2026", "m2", "追缴", "60.00", reason="重复计务工", actor="fin2", on="2027-01-10")
        settled1 = s.member_settlement("PLAN-2026", "m1")
        settled2 = s.member_settlement("PLAN-2026", "m2")
        self.assertEqual(settled1["dividend"], "2470.00")
        self.assertEqual(settled2["dividend"], "1350.00")
        # 原始方案行保持不变
        self.assertEqual(settled1["base_amount"], "2350.00")
        # 草案不可调整，只能先在发布前纠错
        s.draft_plan("PLAN-2027", 2027, as_of="2027-12-31", actor="fin1", on="2028-01-05")
        with self.assertRaisesRegex(RuleViolation, "尚未发布"):
            s.adjust_plan("A4", "PLAN-2027", "m1", "补发", "10.00", reason="x", actor="fin2", on="2028-01-06")

    def test_adjustment_requires_member_in_plan(self) -> None:
        s = self.svc
        s.register_member("mX", "外挂", 0, "2026-01-01", actor="admin")
        s.draft_plan("PLAN-22026", 2026, as_of="2026-12-31", actor="fin1", on="2027-01-05")
        s.publish_plan("PLAN-22026", actor="fin2", on="2027-01-08")
        with self.assertRaises(NotFound):
            s.adjust_plan("A9", "PLAN-22026", "mX", "补发", "10.00", reason="无此人", actor="fin2", on="2027-01-09")

    def test_negative_net_blocks_draft(self) -> None:
        s = self.svc
        s.record_expense("E9", "作业成本", "9000.00", detail="大额机械购置", actor="staff1", on="2026-09-20")
        s.confirm_expense("E9", actor="fin1", on="2026-09-21")
        with self.assertRaisesRegex(RuleViolation, "净收益为负"):
            s.draft_plan("PLAN-BAD", 2026, as_of="2026-12-31", actor="fin1", on="2027-01-05")

    def test_recorder_cannot_confirm_own_expense_or_wage(self) -> None:
        s = self.svc
        s.record_expense("E4", "公益支出", "80.00", detail="自查用支出", actor="staff1", on="2026-12-05")
        with self.assertRaisesRegex(RuleViolation, "录入人与确认人不能是同一人"):
            s.confirm_expense("E4", actor="staff1", on="2026-12-06")
        s.record_wage("W2", "m3", "J1", "120.00", work_days=1.0, actor="staff1", on="2026-09-12")
        with self.assertRaisesRegex(RuleViolation, "录入人与确认人不能是同一人"):
            s.confirm_wage("W2", actor="staff1", on="2026-09-13")

    def test_plan_snapshot_is_immutable_to_later_events(self) -> None:
        s = self.svc
        s.draft_plan("PLAN-2026", 2026, as_of="2026-12-31", actor="fin1", on="2027-01-05")
        before = s.state["plans"]["PLAN-2026"]["net"]
        # 方案形成后于 2027 年发生的尾货销售，不应回改 2026 方案快照
        s.sell_forage("S9", "B1", 100, "0.80", buyer="尾货收购", actor="staff1", on="2027-01-06")
        self.assertEqual(s.state["plans"]["PLAN-2026"]["net"], before)


if __name__ == "__main__":
    unittest.main()
