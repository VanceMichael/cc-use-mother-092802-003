"""测试共用：构建一个完整的 2026 年度运营场景。"""

from __future__ import annotations

from pathlib import Path

from src.grassland import GrasslandService, Ledger


def build_scenario(path: str | Path) -> GrasslandService:
    svc = GrasslandService(Ledger(Path(path)))
    # 地块与边界
    svc.register_plot("P1", "东湾草场", 12000.0, "界桩东1-东42至河岸线", actor="admin", on="2026-08-01")
    svc.register_plot("P2", "西坡草场", 8000.0, "界桩西7-西31接公路", actor="admin", on="2026-08-01")
    # 休牧期
    svc.impose_restriction(
        "R1", "P1", "休牧", "2026-08-20", "2026-09-01",
        reason="拔节期休牧", actor="admin", on="2026-08-15",
    )
    # 生态确认：草籽成熟 + 观察
    svc.confirm_ecology("P1", "2026-09-05", "OBS-P1-0906", observer="obs1", on="2026-09-06")
    # 农机与班组
    svc.register_machine("M1", "收割机一号", actor="admin", on="2026-08-10")
    svc.register_machine("M2", "收割机二号", actor="admin", on="2026-08-10")
    svc.register_crew("C1", "一组", ["m2", "m3"], actor="admin", on="2026-08-10")
    # 成员股份
    svc.register_member("m1", "阿荣", 10, "2026-01-01", actor="admin")
    svc.register_member("m2", "巴特尔", 6, "2026-01-01", actor="admin")
    svc.register_member("m3", "其其格", 4, "2026-01-01", actor="admin")
    # 任务与作业
    svc.open_task("T1", "P1", "2026-09-07", "2026-09-20", actor="admin")
    svc.schedule_job("J1", "T1", "M1", "C1", "2026-09-08", "2026-09-10", actor="admin")
    svc.start_job("J1", actor="foreman", on="2026-09-08")
    svc.complete_job("J1", actor="foreman", on="2026-09-10")
    # 牧草批次：毛 10200 − 皮 200 = 净 10000；损耗 200；入库 9800
    svc.weigh_forage("B1", "T1", 10200, 200, weigher="m2", on="2026-09-10")
    svc.record_loss("B1", 200, reason="搬运碎屑", actor="storeman", on="2026-09-10")
    svc.store_forage("B1", "集体草料库一号棚", actor="storeman", on="2026-09-11")
    # 优惠供应与对外销售
    svc.dispatch_subsidy("D1", "B1", "m1", 1000, "0.30", actor="staff1", on="2026-09-15")
    svc.sell_forage("S1", "B1", 5000, "0.80", buyer="镇饲料站", actor="staff1", on="2026-10-02")
    svc.sell_forage("S2", "B1", 3000, "0.80", buyer="邻嘎查合作社", actor="staff1", on="2026-10-05")
    # 成本、公益支出、务工报酬（双岗确认）
    svc.record_expense("E1", "作业成本", "1000.00", detail="收割燃油与维修", actor="staff1", on="2026-09-25")
    svc.confirm_expense("E1", actor="fin1", on="2026-09-26")
    svc.record_expense("E2", "公益支出", "200.00", detail="代缴合作医疗保险", actor="staff1", on="2026-11-20")
    svc.confirm_expense("E2", actor="fin1", on="2026-11-21")
    svc.record_wage("W1", "m2", "J1", "500.00", work_days=3.0, actor="staff1", on="2026-09-12")
    svc.confirm_wage("W1", actor="fin1", on="2026-09-13")
    return svc
