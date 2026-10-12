"""权限与可见性：谁能看哪个项目、谁是项目成员、审批范围"""
from __future__ import annotations

from flask import flash
from flask_login import current_user

from ..extensions import db
from ..models import Project, ProjectMember, Role, Transaction, User



def is_admin() -> bool:
    return getattr(current_user, "role", None) == Role.ADMIN.value


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


def _check_project_not_ended(project) -> bool:
    """检查项目是否已终止，已终止则 flash 提示并返回 False。"""
    if getattr(project, "status", None) == "ended":
        flash("项目已终止，不可执行此操作", "danger")
        return False
    return True


def _can_view_project(user_id: int, project_id: int) -> bool:
    """用户是否能查看某个项目（穿透父项目链）。"""
    visited = set()
    p = db.session.get(Project, project_id)
    while p:
        if p.id in visited:
            break
        visited.add(p.id)
        if _is_project_member(p.id, user_id):
            return True
        p = p.parent_project
    return False


def _collect_all_sub_project_ids(project_id: int) -> set[int]:
    """递归收集所有子孙项目的 ID。"""
    ids = {project_id}
    for child in Project.query.filter_by(parent_project_id=project_id).all():
        ids |= _collect_all_sub_project_ids(int(child.id))
    return ids


def _can_manage_log_rules(p: Project) -> bool:
    """谁能维护项目的日志撰写规则：管理员或该项目负责人。"""
    if is_admin():
        return True
    return p.leader_user_id is not None and int(p.leader_user_id) == int(current_user.id)


__all__ = [n for n in dir() if not n.startswith("__")]
