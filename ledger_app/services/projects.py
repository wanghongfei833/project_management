"""项目口径：应收合计、状态切换、操作日志"""
from __future__ import annotations

from datetime import datetime, timezone

from flask_login import current_user

from ..extensions import db
from ..models import Project, ProjectActivityLog, ProjectExpectedIncomeAdjustment



def _log_project_activity(
    project_id: int,
    action: str,
    summary: str,
    *,
    detail: str | None = None,
    actor_user_id: int | None = None,
) -> None:
    """写入项目操作日志（与 commit 同一事务中调用）。"""
    if not project_id:
        return
    uid = actor_user_id
    if uid is None and getattr(current_user, "is_authenticated", False):
        uid = int(current_user.id)
    db.session.add(
        ProjectActivityLog(
            project_id=int(project_id),
            action=(action or "misc")[:64],
            summary=(summary or "")[:512],
            detail=detail,
            actor_user_id=uid,
        )
    )


def _apply_project_status(p: Project, new_status: str) -> None:
    """只接受 open/ended；切到 ended 补 ended_at，切回 open 清空。

    历史上表单里有过 "closed" 选项，但全站判定都用 "ended"，写进去会变成
    「看起来已结束、系统仍当进行中」的脏状态，这里统一兜住。
    """
    status = (new_status or "open").strip()
    if status not in ("open", "ended"):
        status = "ended" if status == "closed" else "open"
    if status == "ended":
        if p.ended_at is None or p.status != "ended":
            p.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        p.ended_at = None
    p.status = status


def project_expected_total_cents(project_id: int) -> int:
    base = (
        db.session.query(db.func.coalesce(Project.expected_income_cents, 0))
        .filter(Project.id == project_id)
        .scalar()
        or 0
    )
    extra = (
        db.session.query(
            db.func.coalesce(db.func.sum(ProjectExpectedIncomeAdjustment.amount_cents), 0)
        )
        .filter(
            ProjectExpectedIncomeAdjustment.project_id == project_id,
            ProjectExpectedIncomeAdjustment.status.in_(["active", "executed"]),
        )
        .scalar()
        or 0
    )
    return int(base) + int(extra)


__all__ = [n for n in dir() if not n.startswith("__")]
