"""审批判定：各类申请的「是否已全部同意 / 是否可以执行」"""
from __future__ import annotations

from ..extensions import db
from ..models import (
    ProjectEndDateChangeRequest,
    ProjectEndRequest,
    ProjectExpectedIncomeAdjustment,
    ProjectReviveRequest,
    Role,
    TransactionCreateRequest,
    TransactionDeleteRequest,
    TransactionEditRequest,
    User,
)
from .access import _project_non_admin_member_user_ids, is_admin



def _open_transaction_edit_request(transaction_id: int) -> TransactionEditRequest | None:
    return (
        TransactionEditRequest.query.filter_by(
            transaction_id=transaction_id, status="open"
        )
        .order_by(TransactionEditRequest.id.desc())
        .first()
    )


def _open_transaction_delete_request(transaction_id: int) -> TransactionDeleteRequest | None:
    return (
        TransactionDeleteRequest.query.filter_by(
            transaction_id=transaction_id, status="open"
        )
        .order_by(TransactionDeleteRequest.id.desc())
        .first()
    )


def _transaction_delete_fully_approved(req: TransactionDeleteRequest) -> bool:
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _transaction_delete_can_execute(req: TransactionDeleteRequest) -> bool:
    # admin 绝对权力：可随时执行
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _transaction_delete_fully_approved(req)


def _open_transaction_create_request(transaction_id: int) -> TransactionCreateRequest | None:
    return (
        TransactionCreateRequest.query.filter_by(
            transaction_id=transaction_id, status="open"
        )
        .order_by(TransactionCreateRequest.id.desc())
        .first()
    )


def _transaction_create_fully_approved(req: TransactionCreateRequest) -> bool:
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _transaction_create_can_execute(req: TransactionCreateRequest) -> bool:
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _transaction_create_fully_approved(req)


def _approval_user_ids_include_any_admin(user_ids: set[int]) -> bool:
    """审批记录里是否包含至少一名管理员账号（用于「全员同意 或 管理员同意」）。"""
    if not user_ids:
        return False
    return (
        db.session.query(User.id)
        .filter(User.id.in_(user_ids), User.role == Role.ADMIN.value)
        .first()
        is not None
    )


def _transaction_edit_fully_approved(req: TransactionEditRequest) -> bool:
    """项目全部非 admin 成员已在该修改申请上同意。"""
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _transaction_edit_can_execute(req: TransactionEditRequest) -> bool:
    """审核通过：全体成员同意 或 管理员同意；管理员可随时执行（绝对权力）。"""
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _transaction_edit_fully_approved(req)


def _open_project_end_date_change_request(project_id: int) -> ProjectEndDateChangeRequest | None:
    return (
        ProjectEndDateChangeRequest.query.filter_by(project_id=project_id, status="open")
        .order_by(ProjectEndDateChangeRequest.id.desc())
        .first()
    )


def _project_end_date_change_fully_approved(req: ProjectEndDateChangeRequest) -> bool:
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _project_end_date_change_can_execute(req: ProjectEndDateChangeRequest | None) -> bool:
    if not req:
        return False
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _project_end_date_change_fully_approved(req)


def _open_project_end_request(project_id: int) -> ProjectEndRequest | None:
    return (
        ProjectEndRequest.query.filter_by(project_id=project_id, status="open")
        .order_by(ProjectEndRequest.id.desc())
        .first()
    )


def _project_end_fully_approved(req: ProjectEndRequest) -> bool:
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _project_end_can_execute(req: ProjectEndRequest | None) -> bool:
    if not req:
        return False
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _project_end_fully_approved(req)


def _open_project_revive_request(project_id: int) -> ProjectReviveRequest | None:
    return (
        ProjectReviveRequest.query.filter_by(project_id=project_id, status="open")
        .order_by(ProjectReviveRequest.id.desc())
        .first()
    )


def _project_revive_fully_approved(req: ProjectReviveRequest) -> bool:
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _project_revive_can_execute(req: ProjectReviveRequest | None) -> bool:
    if not req:
        return False
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _project_revive_fully_approved(req)


def _adjustment_can_execute(adj: ProjectExpectedIncomeAdjustment) -> bool:
    """应收追加审核：admin绝对权力 / 有admin已同意 / 非admin成员全部同意。"""
    if is_admin():
        return True
    member_ids = _project_non_admin_member_user_ids(int(adj.project_id))
    approved_ids = {a.user_id for a in adj.approvals}
    if member_ids and member_ids.issubset(approved_ids):
        return True
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return False


__all__ = [n for n in dir() if not n.startswith("__")]
