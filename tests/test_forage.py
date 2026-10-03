"""牧草批次数量守恒：称重、损耗、入库、优惠供应、对外销售。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import build_scenario

from src.grassland import Conflict, GrasslandService, Ledger, RuleViolation


class ForageConservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ops.jsonl"
        self.svc = build_scenario(self.path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_batch_quantities_conserve(self) -> None:
        batch = self.svc.state["batches"]["B1"]
        # 净重 = 毛重 − 皮重；入库 = 净重 − 损耗；结存 = 入库 − 已出库
        self.assertEqual(batch["net_kg"], 10000)
        self.assertEqual(batch["stored_kg"], 9800)
        self.assertEqual(
            batch["stored_kg"],
            batch["net_kg"] - batch["loss_kg"],
        )
        self.assertEqual(
            batch["stored_kg"] - batch["out_subsidy_kg"] - batch["out_sale_kg"],
            800,
        )

    def test_invalid_weights_rejected(self) -> None:
        with self.assertRaisesRegex(RuleViolation, "称重数据无效"):
            self.svc.weigh_forage("BAD", "T1", 100, 200, weigher="m2", on="2026-09-10")

    def test_storage_requires_exact_conservation(self) -> None:
        self.svc.weigh_forage("B2", "T1", 5000, 100, weigher="m2", on="2026-09-10")
        self.svc.record_loss("B2", 100, reason="水分散失", actor="storeman", on="2026-09-10")
        self.svc.store_forage("B2", "二号棚", actor="storeman", on="2026-09-11")
        self.assertEqual(self.svc.state["batches"]["B2"]["stored_kg"], 4800)

    def test_loss_cannot_reach_or_exceed_net(self) -> None:
        self.svc.weigh_forage("B3", "T1", 500, 0, weigher="m2", on="2026-09-10")
        with self.assertRaisesRegex(RuleViolation, "损耗不能达到或超过净重"):
            self.svc.record_loss("B3", 500, reason="霉烂", actor="storeman", on="2026-09-10")

    def test_cannot_dispatch_before_storage(self) -> None:
        self.svc.weigh_forage("B4", "T1", 600, 0, weigher="m2", on="2026-09-10")
        with self.assertRaisesRegex(RuleViolation, "尚未入库"):
            self.svc.sell_forage("Sx", "B4", 100, "0.8", buyer="x", actor="staff1", on="2026-09-11")

    def test_dispatch_cannot_exceed_remaining_stock(self) -> None:
        # B1 结存仅剩 800 千克
        with self.assertRaisesRegex(RuleViolation, "超出批次结存"):
            self.svc.sell_forage("S3", "B1", 801, "0.80", buyer="散客", actor="staff1", on="2026-10-06")
        # 恰好用尽结存可以
        self.svc.sell_forage("S3", "B1", 800, "0.80", buyer="散客", actor="staff1", on="2026-10-06")
        batch = self.svc.state["batches"]["B1"]
        self.assertEqual(batch["out_subsidy_kg"] + batch["out_sale_kg"], batch["stored_kg"])

    def test_offline_backfill_weights_then_restart_conserves(self) -> None:
        # 现场断网：业务 9/10 发生，10/1 才补录
        self.svc.weigh_forage(
            "B5", "T1", 2000, 0, weigher="m2", on="2026-09-10", recorded_date="2026-10-01"
        )
        self.svc.record_loss("B5", 50, reason="补录时核定损耗", actor="storeman", on="2026-09-10", )
        self.svc.store_forage("B5", "一号棚", actor="storeman", on="2026-09-10", )
        revived = GrasslandService(Ledger(self.path))
        self.assertEqual(revived.state["batches"]["B5"]["stored_kg"], 1950)
        weighed = [e for e in revived.entries if e.event_type == "forage.weighed" and e.payload["batch_id"] == "B5"][0]
        self.assertTrue(weighed.backfill)

    def test_duplicate_storage_rejected(self) -> None:
        with self.assertRaisesRegex(Conflict, "重复入库"):
            self.svc.store_forage("B1", "一号棚", actor="storeman", on="2026-09-12")


if __name__ == "__main__":
    unittest.main()
