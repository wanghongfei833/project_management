"""权限与可见性：谁能看哪个项目、谁是项目成员、审批范围"""
from __future__ import annotations

from flask import flash
from flask_login import current_user

from ..extensions import db
from ..models import Project, ProjectMember, Role, Transaction, User



def is_admin() -> bool:
    return getattr(current_user, "role", None) == Role.ADMIN.value


def is_super_admin() -> bool:
    """内置 admin 超级账号：即使其他账号角色也是 admin，也不能管理项目关联。"""
    return bool(
        getattr(current_user, "is_authenticated", False)
        and getattr(current_user, "username", None) == "admin"
        and is_admin()
    )


def _admin_user_ids() -> set[int]:
    rows = db.session.query(User.id).filter(User.role == Role.ADMIN.value).all()
    return {int(r[0]) for r in rows}


def _accessible_projects_query():
    """管理员可查全部项目；普通用户仅可查本人所在项目。"""
    if is_admin():
        return Project.query
    return (
        Project.query.join(ProjectMember, ProjectMember.project_id == Project.id)
        .filter(ProjectMember.user_id == current_user.id)
    )


def _accessible_member_project_ids_query():
    """非管理员：返回本人所在项目的 project_id 查询（单列）；管理员返回 None 表示不限制。"""
    if is_admin():
        return None
    return db.session.query(ProjectMember.project_id).filter(
        ProjectMember.user_id == current_user.id
    )


def _apply_transaction_project_access_filter(q):
    """将流水查询限制为当前用户可访问的项目（管理员不加限制）。"""
    ids_q = _accessible_member_project_ids_query()
    if ids_q is not None:
        q = q.filter(Transaction.project_id.in_(ids_q))
    return q


def _is_project_member(project_id: int, user_id: int) -> bool:
    return (
        ProjectMember.query.filter_by(project_id=project_id, user_id=user_id).first()
        is not None
    )


def _project_member_user_ids(project_id: int) -> set[int]:
    rows = ProjectMember.query.filter_by(project_id=project_id).all()
    return {int(r.user_id) for r in rows}


def _project_non_admin_member_user_ids(project_id: int) -> set[int]:
    """返回项目中去掉 admin 角色的成员 ID 集合（非 admin 成员全部同意即可通过审批，无需 admin 同意）。"""
    rows = (
        db.session.query(ProjectMember.user_id)
        .join(User, User.id == ProjectMember.user_id)
        .filter(
            ProjectMember.project_id == project_id,
            User.role != Role.ADMIN.value,
        )
        .all()
    )
    return {int(r.user_id) for r in rows}


def _project_leader_user_id(project_id: int) -> int | None:
    row = (
        db.session.query(Project.leader_user_id)
        .filter(Project.id == int(project_id))
        .first()
    )
    if not row or row[0] is None:
        return None
    return int(row[0])


def _approval_required_user_ids(project_id: int) -> set[int]:
    """需要同意的关键人：项目负责人。

    新规则：**管理员同意 或 项目负责人同意** 即可（管理员有绝对权力，不在此集合）。
    """
    leader = _project_leader_user_id(int(project_id))
    return {leader} if leader else set()


def _is_project_leader(user_id: int, project_id: int) -> bool:
    return _project_leader_user_id(int(project_id)) == int(user_id)


def _check_project_not_ended(project) -> bool:
    """检查项目是否已终止，已终止则 flash 提示并返回 False。"""
    if getattr(project, "status", None) == "ended":
        flash("项目已终止，不可执行此操作", "danger")
        return False
    return True


def _can_view_project(user_id: int, project_id: int) -> bool:
    """项目权限严格隔离：只允许当前项目成员，父子关系不授予访问权。"""
    return _is_project_member(int(project_id), int(user_id))


def _collect_all_sub_project_ids(
    project_id: int, _visited: set[int] | None = None
) -> set[int]:
    """递归收集所有子孙项目的 ID。"""
    visited = _visited if _visited is not None else set()
    if project_id in visited:
        return set()
    visited.add(project_id)
    ids = {project_id}
    for child in Project.query.filter_by(parent_project_id=project_id).all():
        ids |= _collect_all_sub_project_ids(int(child.id), visited)
    return ids


def _can_manage_log_rules(p: Project) -> bool:
    """谁能维护项目的日志撰写规则：管理员或该项目负责人。"""
    if is_admin():
        return True
    return p.leader_user_id is not None and int(p.leader_user_id) == int(current_user.id)


__all__ = [n for n in dir() if not n.startswith("__")]
