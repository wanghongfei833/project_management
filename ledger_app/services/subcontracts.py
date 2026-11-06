"""管理员专属的隐藏项目关联与动态应收汇总。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from flask_login import current_user

from ..extensions import db
from ..models import (
    Project,
    ProjectExpectedIncomeAdjustment,
    ProjectLink,
)


@dataclass(frozen=True)
class LinkedProjectRow:
    link: ProjectLink
    child: Project
    current_receivable_cents: int


@dataclass(frozen=True)
class ProjectLinkSummary:
    project: Project | None
    base_receivable_cents: int
    linked_receivable_cents: int
    total_receivable_cents: int
    children: list[LinkedProjectRow]


def base_project_receivable_cents(project_id: int) -> int:
    project = db.session.get(Project, int(project_id))
    if project is None:
        return 0
    extra = int(
        db.session.query(
            db.func.coalesce(
                db.func.sum(ProjectExpectedIncomeAdjustment.amount_cents), 0
            )
        )
        .filter(
            ProjectExpectedIncomeAdjustment.project_id == int(project_id),
            ProjectExpectedIncomeAdjustment.status.in_(["active", "executed"]),
        )
        .scalar()
        or 0
    )
    return int(project.expected_income_cents or 0) + extra


def active_child_links(parent_project_id: int) -> list[ProjectLink]:
    return (
        ProjectLink.query.filter_by(
            parent_project_id=int(parent_project_id),
            status="active",
        )
        .order_by(ProjectLink.id.asc())
        .all()
    )


def active_parent_link(child_project_id: int) -> ProjectLink | None:
    return (
        ProjectLink.query.filter_by(
            child_project_id=int(child_project_id),
            status="active",
        )
        .order_by(ProjectLink.id.desc())
        .first()
    )


def project_link_summary(project_id: int) -> ProjectLinkSummary:
    project = db.session.get(Project, int(project_id))
    if project is None:
        return ProjectLinkSummary(None, 0, 0, 0, [])
    base = base_project_receivable_cents(int(project.id))
    children: list[LinkedProjectRow] = []
    for link in active_child_links(int(project.id)):
        child = db.session.get(Project, int(link.child_project_id))
        if child is None:
            continue
        children.append(
            LinkedProjectRow(
                link=link,
                child=child,
                current_receivable_cents=base_project_receivable_cents(
                    int(child.id)
                ),
            )
        )
    linked = sum(row.current_receivable_cents for row in children)
    return ProjectLinkSummary(
        project=project,
        base_receivable_cents=base,
        linked_receivable_cents=linked,
        total_receivable_cents=base + linked,
        children=children,
    )


def project_total_receivable_cents(project_id: int) -> int:
    return project_link_summary(project_id).total_receivable_cents


def would_create_link_cycle(parent_project_id: int, child_project_id: int) -> bool:
    """沿父链向上检查，防止 A→B→A 等循环。"""
    target = int(child_project_id)
    current = int(parent_project_id)
    visited: set[int] = set()
    while current:
        if current in visited:
            return True
        visited.add(current)
        if current == target:
            return True
        link = active_parent_link(current)
        if link is None:
            break
        current = int(link.parent_project_id)
    return False


def attach_project_link(
    parent_project_id: int,
    child_project_id: int,
    *,
    start_date: date | None = None,
) -> ProjectLink:
    parent = db.session.get(Project, int(parent_project_id))
    child = db.session.get(Project, int(child_project_id))
    if parent is None or child is None:
        raise ValueError("项目不存在")
    if int(parent.id) == int(child.id):
        raise ValueError("项目不能关联自身")
    if active_parent_link(int(child.id)) is not None:
        raise ValueError("该子项目已经存在生效中的父项目关联")
    if would_create_link_cycle(int(parent.id), int(child.id)):
        raise ValueError("该关联会导致循环关系")

    link = ProjectLink(
        parent_project_id=int(parent.id),
        child_project_id=int(child.id),
        status="active",
        start_date=start_date or date.today(),
        created_by_user_id=(
            int(current_user.id)
            if getattr(current_user, "is_authenticated", False)
            else None
        ),
    )
    db.session.add(link)
    db.session.flush()
    return link


__all__ = [name for name in dir() if not name.startswith("__")]
