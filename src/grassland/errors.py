"""集体草场运营服务的领域异常。"""

from __future__ import annotations


class DomainError(Exception):
    """所有业务规则冲突的基类，信息直接面向录入人员。"""


class NotFound(DomainError):
    """引用的记录不存在。"""


class Conflict(DomainError):
    """状态或资源占用冲突，例如地块同一时段重复排产。"""


class RuleViolation(DomainError):
    """违反生态时序、数量守恒或职责分离等硬性规则。"""


class LedgerError(DomainError):
    """账本被篡改、断链或无法重放。"""
