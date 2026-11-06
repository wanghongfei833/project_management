"""「待我处理」审批事项汇总。

把全站需要审批的申请收敛到一处：新增/修改/删除流水、追加应收、
项目终止/复活/删除、计划结束日期变更。

判定口径与各 approve 路由保持一致：**项目负责人或管理员任一同意即通过**。
只有负责人、管理员以及申请人本人能看到对应申请；申请人发起时会自动记一条同意记录。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from flask import url_for

from .extensions import db
from .models import (
    Project,
    ProjectAdjustmentApproval,
    ProjectDeleteApproval,
    ProjectDeleteRequest,
    ProjectEndApproval,
    ProjectEndDateChangeApproval,
    ProjectEndDateChangeRequest,
    ProjectEndRequest,
    ProjectExpectedIncomeAdjustment,
    ProjectMember,
    ProjectReviveApproval,
    ProjectReviveRequest,
    Role,
    Transaction,
    TransactionCreateApproval,
    TransactionCreateRequest,
    TransactionDeleteApproval,
    TransactionDeleteRequest,
    TransactionEditApproval,
    TransactionEditRequest,
    TransactionType,
    User,
)
from .utils import format_cents
from .services.fund_pool import fund_account_label


def _url(endpoint: str, **values) -> str:
    """生成站内链接；定时任务等无请求上下文时返回空串（短信正文里用不到链接）。"""
    try:
        return url_for(endpoint, **values)
    except RuntimeError:
        return ""


@dataclass(frozen=True)
class PendingItem:
    kind: str
    label: str
    project_id: int
    project_name: str
    summary: str
    requester: str
    requester_id: int | None
    created_at: datetime | None
    approve_url: str
    detail_url: str
    approved_by_me: bool
    approved_count: int
    required_count: int


def _type_label(tx_type: str) -> str:
    return "收入" if tx_type == TransactionType.INCOME.value else "支出"


def _fmt_date(value) -> str:
    return value.strftime("%Y-%m-%d") if value else "—"


def _usernames(user_ids) -> dict[int, str]:
    ids = {int(i) for i in user_ids if i}
    if not ids:
        return {}
    rows = db.session.query(User.id, User.username).filter(User.id.in_(ids)).all()
    return {int(uid): name for uid, name in rows}


def _member_project_ids(user_id: int) -> set[int]:
    rows = (
        db.session.query(ProjectMember.project_id)
        .filter(ProjectMember.user_id == user_id)
        .all()
    )
    return {int(r[0]) for r in rows}


def _approvals_by_request(model, key_name: str, key_values) -> dict[int, set[int]]:
    """{请求 id: {已同意的 user_id}}。"""
    values = [int(v) for v in key_values]
    if not values:
        return {}
    key_col = getattr(model, key_name)
    rows = db.session.query(key_col, model.user_id).filter(key_col.in_(values)).all()
    out: dict[int, set[int]] = {}
    for key, user_id in rows:
        out.setdefault(int(key), set()).add(int(user_id))
    return out


def pending_items_for_user(user_id: int, *, is_admin_user: bool) -> list[PendingItem]:
    """返回该用户作为「审批人」或「发起人」可见的在途申请。

    新审批规则：**管理员同意 或 项目负责人同意** 即通过。
    所以只有负责人/管理员会作为审批人看到待办；其他成员只有在是发起人时
    才会在「我已同意，等待负责人处理」里看到自己发起的申请。
    """
    uid = int(user_id)
    leaders = {
        int(pid): (int(lid) if lid else None)
        for pid, lid in db.session.query(Project.id, Project.leader_user_id).all()
    }
    admin_ids = {
        int(uid_)
        for (uid_,) in db.session.query(User.id)
        .filter(User.role == Role.ADMIN.value)
        .all()
    }
    leader_pids = {pid for pid, lid in leaders.items() if lid == uid}
    my_project_ids = (
        None if is_admin_user else (_member_project_ids(uid) | leader_pids)
    )

    def _required_count(pid) -> int:
        return 1 if leaders.get(int(pid)) else 0

    def _approved_count(pid, approved_ids) -> int:
        leader = leaders.get(int(pid))
        if not leader:
            return 0
        return 1 if (leader in approved_ids or (approved_ids & admin_ids)) else 0

    def in_scope(project_id) -> bool:
        if is_admin_user:
            return True
        return my_project_ids is not None and int(project_id) in my_project_ids

    items: list[PendingItem] = []

    # ---- 1. 新增流水 ----
    rows = (
        db.session.query(Transaction, TransactionCreateRequest, Project)
        .join(
            TransactionCreateRequest,
            TransactionCreateRequest.transaction_id == Transaction.id,
        )
        .join(Project, Project.id == Transaction.project_id)
        .filter(
            TransactionCreateRequest.status == "open",
            Transaction.is_void.is_(False),
            Transaction.status == "pending",
        )
        .all()
    )
    rows = [r for r in rows if in_scope(r[2].id)]
    if rows:
        approvals = _approvals_by_request(
            TransactionCreateApproval, "request_id", [r[1].id for r in rows]
        )
        names = _usernames({r[1].created_by_user_id for r in rows})
        for tx, req, project in rows:
            approved = approvals.get(int(req.id), set())
            items.append(
                PendingItem(
                    kind="tx_create",
                    label="新增流水",
                    project_id=int(project.id),
                    project_name=project.name,
                    summary=(
                        f"#{tx.id} {_type_label(tx.type)} "
                        f"¥{format_cents(tx.amount_cents)}，{_fmt_date(tx.occur_date)}"
                        f" · {fund_account_label(tx.fund_account)}"
                    ),
                    requester=names.get(int(req.created_by_user_id or 0), "—"),
                    requester_id=int(req.created_by_user_id or 0),
                    created_at=req.created_at,
                    approve_url=_url(
                        "main.transactions_create_approve", transaction_id=tx.id
                    ),
                    detail_url=_url("main.project_detail", project_id=project.id),
                    approved_by_me=uid in approved,
                    approved_count=_approved_count(int(project.id), approved),
                    required_count=_required_count(int(project.id)),
                )
            )

    # ---- 2. 修改流水 ----
    rows = (
        db.session.query(TransactionEditRequest, Transaction, Project)
        .join(Transaction, Transaction.id == TransactionEditRequest.transaction_id)
        .join(Project, Project.id == TransactionEditRequest.project_id)
        .filter(
            TransactionEditRequest.status == "open",
            Transaction.is_void.is_(False),
        )
        .all()
    )
    rows = [r for r in rows if in_scope(r[2].id)]
    if rows:
        approvals = _approvals_by_request(
            TransactionEditApproval, "request_id", [r[0].id for r in rows]
        )
        names = _usernames({r[0].created_by_user_id for r in rows})
        for req, tx, project in rows:
            approved = approvals.get(int(req.id), set())
            items.append(
                PendingItem(
                    kind="tx_edit",
                    label="修改流水",
                    project_id=int(project.id),
                    project_name=project.name,
                    summary=(
                        f"#{tx.id} {_type_label(tx.type)} "
                        f"¥{format_cents(tx.amount_cents)} → "
                        f"{_type_label(req.new_type)} ¥{format_cents(req.new_amount_cents)}，"
                        f"{_fmt_date(req.new_occur_date)}"
                        f" · {fund_account_label(req.new_fund_account)}"
                    ),
                    requester=names.get(int(req.created_by_user_id or 0), "—"),
                    requester_id=int(req.created_by_user_id or 0),
                    created_at=req.created_at,
                    approve_url=_url(
                        "main.transactions_edit_approve", transaction_id=tx.id
                    ),
                    detail_url=_url("main.project_detail", project_id=project.id),
                    approved_by_me=uid in approved,
                    approved_count=_approved_count(int(project.id), approved),
                    required_count=_required_count(int(project.id)),
                )
            )

    # ---- 3. 删除流水 ----
    rows = (
        db.session.query(TransactionDeleteRequest, Transaction, Project)
        .join(Transaction, Transaction.id == TransactionDeleteRequest.transaction_id)
        .join(Project, Project.id == TransactionDeleteRequest.project_id)
        .filter(
            TransactionDeleteRequest.status == "open",
            Transaction.is_void.is_(False),
        )
        .all()
    )
    rows = [r for r in rows if in_scope(r[2].id)]
    if rows:
        approvals = _approvals_by_request(
            TransactionDeleteApproval, "request_id", [r[0].id for r in rows]
        )
        names = _usernames({r[0].created_by_user_id for r in rows})
        for req, tx, project in rows:
            approved = approvals.get(int(req.id), set())
            items.append(
                PendingItem(
                    kind="tx_delete",
                    label="删除流水",
                    project_id=int(project.id),
                    project_name=project.name,
                    summary=(
                        f"#{tx.id} {_type_label(tx.type)} "
                        f"¥{format_cents(tx.amount_cents)}，{_fmt_date(tx.occur_date)}"
                        f" · {fund_account_label(tx.fund_account)}"
                    ),
                    requester=names.get(int(req.created_by_user_id or 0), "—"),
                    requester_id=int(req.created_by_user_id or 0),
                    created_at=req.created_at,
                    approve_url=_url(
                        "main.transactions_delete_approve", transaction_id=tx.id
                    ),
                    detail_url=_url("main.project_detail", project_id=project.id),
                    approved_by_me=uid in approved,
                    approved_count=_approved_count(int(project.id), approved),
                    required_count=_required_count(int(project.id)),
                )
            )

    # ---- 4. 追加应收 ----
    rows = (
        db.session.query(ProjectExpectedIncomeAdjustment, Project)
        .join(Project, Project.id == ProjectExpectedIncomeAdjustment.project_id)
        .filter(ProjectExpectedIncomeAdjustment.status == "pending")
        .all()
    )
    rows = [r for r in rows if in_scope(r[1].id)]
    if rows:
        approvals = _approvals_by_request(
            ProjectAdjustmentApproval, "adjustment_id", [r[0].id for r in rows]
        )
        names = _usernames({r[0].created_by_user_id for r in rows})
        for adj, project in rows:
            approved = approvals.get(int(adj.id), set())
            note = f"（{adj.note}）" if adj.note else ""
            items.append(
                PendingItem(
                    kind="adjust",
                    label="追加应收",
                    project_id=int(project.id),
                    project_name=project.name,
                    summary=f"追加 ¥{format_cents(adj.amount_cents)}{note}",
                    requester=names.get(int(adj.created_by_user_id or 0), "—"),
                    requester_id=int(adj.created_by_user_id or 0),
                    created_at=adj.created_at,
                    approve_url=_url(
                        "main.project_adjust_approve",
                        project_id=project.id,
                        adjustment_id=adj.id,
                    ),
                    detail_url=_url("main.project_detail", project_id=project.id),
                    approved_by_me=uid in approved,
                    approved_count=_approved_count(int(project.id), approved),
                    required_count=_required_count(int(project.id)),
                )
            )

    # ---- 5. 项目终止 ----
    rows = (
        db.session.query(ProjectEndRequest, Project)
        .join(Project, Project.id == ProjectEndRequest.project_id)
        .filter(ProjectEndRequest.status == "open")
        .all()
    )
    rows = [r for r in rows if in_scope(r[1].id)]
    if rows:
        approvals = _approvals_by_request(
            ProjectEndApproval, "request_id", [r[0].id for r in rows]
        )
        names = _usernames({r[0].created_by_user_id for r in rows})
        for req, project in rows:
            approved = approvals.get(int(req.id), set())
            items.append(
                PendingItem(
                    kind="project_end",
                    label="项目终止",
                    project_id=int(project.id),
                    project_name=project.name,
                    summary="终止项目，通过后冻结为只读",
                    requester=names.get(int(req.created_by_user_id or 0), "—"),
                    requester_id=int(req.created_by_user_id or 0),
                    created_at=req.created_at,
                    approve_url=_url(
                        "main.project_end_approve", project_id=project.id
                    ),
                    detail_url=_url("main.project_detail", project_id=project.id),
                    approved_by_me=uid in approved,
                    approved_count=_approved_count(int(project.id), approved),
                    required_count=_required_count(int(project.id)),
                )
            )

    # ---- 6. 项目复活 ----
    rows = (
        db.session.query(ProjectReviveRequest, Project)
        .join(Project, Project.id == ProjectReviveRequest.project_id)
        .filter(ProjectReviveRequest.status == "open")
        .all()
    )
    rows = [r for r in rows if in_scope(r[1].id)]
    if rows:
        approvals = _approvals_by_request(
            ProjectReviveApproval, "request_id", [r[0].id for r in rows]
        )
        names = _usernames({r[0].created_by_user_id for r in rows})
        for req, project in rows:
            approved = approvals.get(int(req.id), set())
            items.append(
                PendingItem(
                    kind="project_revive",
                    label="项目复活",
                    project_id=int(project.id),
                    project_name=project.name,
                    summary="复活项目，通过后恢复为进行中",
                    requester=names.get(int(req.created_by_user_id or 0), "—"),
                    requester_id=int(req.created_by_user_id or 0),
                    created_at=req.created_at,
                    approve_url=_url(
                        "main.project_revive_approve", project_id=project.id
                    ),
                    detail_url=_url("main.project_detail", project_id=project.id),
                    approved_by_me=uid in approved,
                    approved_count=_approved_count(int(project.id), approved),
                    required_count=_required_count(int(project.id)),
                )
            )

    # ---- 7. 项目删除 ----
    rows = (
        db.session.query(ProjectDeleteRequest, Project)
        .join(Project, Project.id == ProjectDeleteRequest.project_id)
        .filter(ProjectDeleteRequest.status == "open")
        .all()
    )
    rows = [r for r in rows if in_scope(r[1].id)]
    if rows:
        approvals = _approvals_by_request(
            ProjectDeleteApproval, "request_id", [r[0].id for r in rows]
        )
        names = _usernames({r[0].created_by_user_id for r in rows})
        for req, project in rows:
            approved = approvals.get(int(req.id), set())
            items.append(
                PendingItem(
                    kind="project_delete",
                    label="删除项目",
                    project_id=int(project.id),
                    project_name=project.name,
                    summary="删除项目及其全部流水与凭证（不可恢复）",
                    requester=names.get(int(req.created_by_user_id or 0), "—"),
                    requester_id=int(req.created_by_user_id or 0),
                    created_at=req.created_at,
                    approve_url=_url(
                        "main.projects_delete_approve", project_id=project.id
                    ),
                    detail_url=_url("main.project_detail", project_id=project.id),
                    approved_by_me=uid in approved,
                    approved_count=_approved_count(int(project.id), approved),
                    required_count=_required_count(int(project.id)),
                )
            )

    # ---- 8. 计划结束日期变更 ----
    rows = (
        db.session.query(ProjectEndDateChangeRequest, Project)
        .join(Project, Project.id == ProjectEndDateChangeRequest.project_id)
        .filter(ProjectEndDateChangeRequest.status == "open")
        .all()
    )
    rows = [r for r in rows if in_scope(r[1].id)]
    if rows:
        approvals = _approvals_by_request(
            ProjectEndDateChangeApproval, "request_id", [r[0].id for r in rows]
        )
        names = _usernames({r[0].created_by_user_id for r in rows})
        for req, project in rows:
            approved = approvals.get(int(req.id), set())
            items.append(
                PendingItem(
                    kind="project_end_date",
                    label="结束日期变更",
                    project_id=int(project.id),
                    project_name=project.name,
                    summary=(
                        f"{_fmt_date(req.old_end_date)} → {_fmt_date(req.new_end_date)}"
                    ),
                    requester=names.get(int(req.created_by_user_id or 0), "—"),
                    requester_id=int(req.created_by_user_id or 0),
                    created_at=req.created_at,
                    approve_url=_url(
                        "main.project_end_date_change_approve", project_id=project.id
                    ),
                    detail_url=_url("main.project_detail", project_id=project.id),
                    approved_by_me=uid in approved,
                    approved_count=_approved_count(int(project.id), approved),
                    required_count=_required_count(int(project.id)),
                )
            )

    # 只保留：我是审批人（负责人/管理员）的，或我是发起人的
    items = [
        it
        for it in items
        if is_admin_user or it.project_id in leader_pids or it.requester_id == uid
    ]
    # 新的排前面
    items.sort(
        key=lambda i: (i.created_at is not None, i.created_at or datetime.min),
        reverse=True,
    )
    return items
