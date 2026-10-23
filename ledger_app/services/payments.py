"""付款闭环：待打款 / 待补回单 / 待确认到账 / 台账查询。

状态机（仅支出；收入侧用「确认到账」）：
    pending（待审批）→ approved（已批准待付款）→ paid（已付款待回单）→ closed（闭环）
    任一阶段可 rejected（驳回）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from flask import url_for
from flask_login import current_user

from ..extensions import db
from ..models import Project, ProjectMember, Role, Transaction, TransactionType, User
from .access import _accessible_projects_query, is_admin

PAYMENT_PENDING = "pending"
PAYMENT_APPROVED = "approved"
PAYMENT_PAID = "paid"
PAYMENT_CLOSED = "closed"
PAYMENT_REJECTED = "rejected"

PAYMENT_LABELS = {
    PAYMENT_PENDING: "待审批",
    PAYMENT_APPROVED: "待打款",
    PAYMENT_PAID: "待补回单",
    PAYMENT_CLOSED: "已闭环",
    PAYMENT_REJECTED: "已驳回",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def can_pay() -> bool:
    """财务权限：管理员默认拥有；普通用户可在用户管理里勾「财务」。"""
    return is_admin() or bool(getattr(current_user, "is_finance", False))


@dataclass(frozen=True)
class PayTodo:
    tx: Transaction
    kind: str          # payment / receipt / income
    tx_id: int
    project_id: int | None
    project_name: str
    amount_cents: int
    requester: str
    created_at: datetime | None
    url: str


def _project_ids_scope(user: User | None = None):
    """某用户可访问的项目 id 集合（管理员 None 表示不限）。"""
    if user is not None and user.role == Role.ADMIN.value:
        return None
    if user is None and is_admin():
        return None
    user_id = int(user.id) if user is not None else int(current_user.id)
    return {
        int(pid)
        for (pid,) in db.session.query(ProjectMember.project_id)
        .filter(ProjectMember.user_id == user_id)
        .all()
    }


def _qs(*filters, scope=None):
    q = Transaction.query.filter(*filters)
    if scope is not None:
        q = q.filter(Transaction.project_id.in_(scope))
    return q


def payable_expenses(*, scope=None):
    """已批准、还未付款的支出。"""
    return (
        _qs(
            Transaction.type == TransactionType.EXPENSE.value,
            Transaction.status == "active",
            Transaction.is_void.is_(False),
            Transaction.settled.is_(False),
            Transaction.payment_status == PAYMENT_APPROVED,
            scope=scope,
        )
        .order_by(Transaction.created_at.asc(), Transaction.id.asc())
        .all()
    )


def receipts_pending(*, scope=None, created_by=None):
    """已付款/已到账、但还没有凭证的交易。"""
    q = _qs(
        Transaction.is_void.is_(False),
        Transaction.settled.is_(True),
        Transaction.payment_status == PAYMENT_PAID,
        scope=scope,
    )
    if created_by is not None:
        q = q.filter(Transaction.created_by_user_id == int(created_by))
    return q.order_by(Transaction.paid_at.asc(), Transaction.id.asc()).all()


def income_awaiting_confirm(*, scope=None):
    """已生效但还没到账的收入（等财务确认到账）。"""
    return (
        _qs(
            Transaction.type == TransactionType.INCOME.value,
            Transaction.status == "active",
            Transaction.is_void.is_(False),
            Transaction.settled.is_(False),
            scope=scope,
        )
        .order_by(Transaction.created_at.asc(), Transaction.id.asc())
        .all()
    )


def _display_name(tx: Transaction) -> str:
    if tx.project and tx.project.name:
        return tx.project.name
    return f"项目#{tx.project_id}"


def _todo(kind: str, tx: Transaction, user_map: dict[int, str]) -> PayTodo:
    return PayTodo(
        tx=tx,
        kind=kind,
        tx_id=int(tx.id),
        project_id=int(tx.project_id) if tx.project_id else None,
        project_name=_display_name(tx),
        amount_cents=int(tx.amount_cents or 0),
        requester=user_map.get(int(tx.created_by_user_id or 0), "—"),
        created_at=tx.created_at,
        url=url_for("main.finance_page", focus=tx.id) if _has_request_context() else "",
    )


def _has_request_context() -> bool:
    from flask import has_request_context

    return has_request_context()


def finance_todos(*, user_id: int, finance: bool = False) -> dict[str, list[PayTodo]]:
    """当前用户（或指定用户）的付款闭环待办。

    finance=True：查看全部待打款/待补回单/待确认到账（管理员/财务）；
    否则只看自己发起的待补回单。
    """
    user = db.session.get(User, int(user_id))
    if user is None:
        return {"payments": [], "receipts": [], "income": []}
    scope = _project_ids_scope(user)

    pay_rows = payable_expenses(scope=scope) if finance else []
    income_rows = income_awaiting_confirm(scope=scope) if finance else []
    if finance:
        receipt_rows = receipts_pending(scope=scope)
    else:
        receipt_rows = receipts_pending(scope=scope, created_by=user_id)

    ids = {
        int(tx.created_by_user_id)
        for tx in (pay_rows + receipt_rows + income_rows)
        if tx.created_by_user_id
    }
    user_map = {
        int(uid): name
        for uid, name in db.session.query(User.id, User.username)
        .filter(User.id.in_(ids))
        .all()
    } if ids else {}

    return {
        "payments": [_todo("payment", tx, user_map) for tx in pay_rows],
        "receipts": [_todo("receipt", tx, user_map) for tx in receipt_rows],
        "income": [_todo("income", tx, user_map) for tx in income_rows],
    }


def payment_status_label(tx: Transaction) -> str:
    """页面上展示的闭环状态（兼容历史数据）。"""
    if tx.payment_status in PAYMENT_LABELS:
        return PAYMENT_LABELS[tx.payment_status]
    if tx.type == TransactionType.INCOME.value:
        return "已到账" if tx.settled else "未到账"
    return "已付款" if tx.settled else "未付款"


def pending_amount_by_project(*, expense: bool = True) -> dict[int, int]:
    """各项目「待打款」或「待补回单」的金额合计（用于项目汇总）。"""
    q = (
        db.session.query(Transaction.project_id, db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(
            Transaction.is_void.is_(False),
            Transaction.payment_status == (PAYMENT_APPROVED if expense else PAYMENT_PAID),
        )
        .group_by(Transaction.project_id)
    )
    return {int(pid): int(total or 0) for pid, total in q.all() if pid}
