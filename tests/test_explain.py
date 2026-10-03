"""溯源解释：分红、优惠牧草、务工收入都能列出完整证据链。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import build_scenario

from src.grassland import explain_dividend, explain_subsidy, explain_wage
from src.grassland.service import GrasslandService
from src.grassland.ledger import Ledger


class ExplainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ops.jsonl"
        self.svc = build_scenario(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dividend_explanation_covers_shares_income_expenses_plan(self) -> None:
        s = self.svc
        s.draft_plan("PLAN-2026", 2026, as_of="2026-12-31", actor="fin1", on="2027-01-05")
        s.publish_plan("PLAN-2026", actor="fin2", on="2027-01-08")
        s.adjust_plan("A1", "PLAN-2026", "m1", "补发", "120.00", reason="漏算配股", actor="fin2", on="2027-01-10")
        text = explain_dividend(s, "PLAN-2026", "m1").render_text()
        for keyword in ("入社股份登记", "对外销售", "已确认作业成本", "已确认公益支出", "方案草案", "方案发布", "发布后补发", "2470.00"):
            self.assertIn(keyword, text)

    def test_subsidy_explanation_traces_to_batch_task_plot(self) -> None:
        s = self.svc
        text = explain_subsidy(s, "D1").render_text()
        for keyword in ("批次称重", "损耗登记", "入库确认", "优惠供应出库", "东湾", "T1", "数量守恒"):
            self.assertIn(keyword, text)

    def test_wage_explanation_covers_lifecycle_and_dual_confirmation(self) -> None:
        s = self.svc
        text = explain_wage(s, "W1").render_text()
        for keyword in ("生态前置确认", "收割任务", "排产", "开工", "完工", "务工报酬登记", "独立确认", "500.00"):
            self.assertIn(keyword, text)

    def test_wage_explanation_flags_unconfirmed(self) -> None:
        s = self.svc
        s.record_wage("W2", "m3", "J1", "200.00", work_days=2.0, actor="staff1", on="2026-09-12")
        text = explain_wage(s, "W2").render_text()
        self.assertIn("尚待第二人确认", text)
        self.assertNotIn("已确认计入", text)

    def test_all_evidence_seq_are_real_and_ordered(self) -> None:
        s = self.svc
        s.draft_plan("PLAN-2026", 2026, as_of="2026-12-31", actor="fin1", on="2027-01-05")
        s.publish_plan("PLAN-2026", actor="fin2", on="2027-01-08")
        for explanation in (
            explain_dividend(s, "PLAN-2026", "m2"),
            explain_subsidy(s, "D1"),
            explain_wage(s, "W1"),
        ):
            seqs = [item.seq for item in explanation.evidence]
            self.assertEqual(seqs, sorted(seqs))
            self.assertTrue(seqs)
            self.assertTrue(all(seq > 0 for seq in seqs))

    def test_backfill_flag_renders_in_text(self) -> None:
        s = self.svc
        s.weigh_forage("B9", "T1", 500, 0, weigher="m2", on="2026-09-10", recorded_date="2026-10-01")
        s.record_loss("B9", 10, reason="补录损耗", actor="storeman", on="2026-09-10")
        s.store_forage("B9", "一号棚", actor="storeman", on="2026-09-10")
        s.dispatch_subsidy("D9", "B9", "m1", 100, "0.30", actor="staff1", on="2026-10-02")
        text = explain_subsidy(s, "D9").render_text()
        self.assertIn("离线补录", text)

    def test_explanation_survives_restart(self) -> None:
        s = self.svc
        s.draft_plan("PLAN-2026", 2026, as_of="2026-12-31", actor="fin1", on="2027-01-05")
        s.publish_plan("PLAN-2026", actor="fin2", on="2027-01-08")
        revived = GrasslandService(Ledger(self.path))
        text = explain_dividend(revived, "PLAN-2026", "m1").render_text()
        self.assertIn("4700.00", text)


if __name__ == "__main__":
    unittest.main()
