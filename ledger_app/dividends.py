"""分红口径的唯一实现。

历史上「可分红基数 / 已分红 / 剩余可分红」在仪表盘、分红页 GET、分红页 POST、
项目详情页各写了一遍，很容易改漏（曾经就因此出过口径 bug）。这里统一成一处。

口径（与 README 一致）：
- 可分红基数 = max(我方已收净额 − 非分红支出, 0)
- 已分红     = max(分红明细表合计, [DIVIDEND] 支出流水合计)   # 两种来源都认，避免重复扣
- 剩余可分红 = max(可分红基数 − 已分红, 0)
"""
from __future__ import annotations

from dataclasses import dataclass

from .extensions import db
from .models import ProjectDividendDistribution, Transaction, TransactionType

DIVIDEND_NOTE_PREFIX = "[DIVIDEND]"


@dataclass(frozen=True)
class DividendMetrics:
    expense_total_cents: int
    dividend_expense_cents: int
    paid_excl_dividend_cents: int
    base_cents: int
    paid_cents: int
    remaining_cents: int


def dividend_metrics(project_id: int, *, received_net_cents: int) -> DividendMetrics:
    """算出某个项目的分红口径指标。received_net_cents 由 build_project_finance 提供。"""
    pid = int(project_id)
    expense_filter = (
        Transaction.project_id == pid,
        Transaction.type == TransactionType.EXPENSE.value,
        Transaction.settled.is_(True),
        Transaction.is_void.is_(False),
        Transaction.status == "active",
    )

    def _sum(*extra) -> int:
        return int(
            db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
            .filter(*expense_filter, *extra)
            .scalar()
            or 0
        )

    expense_total = _sum()
    dividend_expense = _sum(
        db.func.coalesce(Transaction.note, "").like(DIVIDEND_NOTE_PREFIX + "%")
    )
    paid_excl_dividend = max(expense_total - dividend_expense, 0)
    base = max(int(received_net_cents) - paid_excl_dividend, 0)
    distributed = int(
        db.session.query(
            db.func.coalesce(db.func.sum(ProjectDividendDistribution.amount_cents), 0)
        )
        .filter(ProjectDividendDistribution.project_id == pid)
        .scalar()
        or 0
    )
    paid = max(distributed, dividend_expense)
    return DividendMetrics(
        expense_total_cents=expense_total,
        dividend_expense_cents=dividend_expense,
        paid_excl_dividend_cents=paid_excl_dividend,
        base_cents=base,
        paid_cents=paid,
        remaining_cents=max(base - paid, 0),
    )
