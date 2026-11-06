"""内置 admin 专属的隐藏项目关联管理。"""
from ._common import *  # noqa: F401,F403


def _require_super_admin():
    if not is_super_admin():
        flash("只有内置 admin 可以管理项目关联", "danger")
        return False
    return True


@bp.get("/admin/project-links")
@login_required
def project_links_admin():
    if not _require_super_admin():
        return redirect(url_for("main.dashboard"))
    projects = Project.query.order_by(Project.name.asc()).all()
    links = (
        ProjectLink.query.order_by(
            ProjectLink.status.asc(), ProjectLink.id.desc()
        ).all()
    )
    parent_summaries = {
        int(p.id): project_link_summary(int(p.id)) for p in projects
    }
    return render_template(
        "project_links_admin.html",
        projects=projects,
        links=links,
        parent_summaries=parent_summaries,
        today=date.today(),
        is_super_admin=True,
    )


@bp.post("/admin/project-links/attach")
@login_required
def project_link_attach():
    if not _require_super_admin():
        return redirect(url_for("main.dashboard"))
    parent_id = request.form.get("parent_project_id", type=int)
    child_id = request.form.get("child_project_id", type=int)
    if not parent_id or not child_id:
        flash("请选择父项目和子项目", "warning")
        return redirect(url_for("main.project_links_admin"))
    try:
        start = request.form.get("start_date", "").strip()
        start_date = date.fromisoformat(start) if start else date.today()
        link = attach_project_link(parent_id, child_id, start_date=start_date)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("main.project_links_admin"))

    parent = db.session.get(Project, parent_id)
    child = db.session.get(Project, child_id)
    _log_project_activity(
        int(parent.id),
        "project_link.attach",
        f"隐藏关联项目：{child.name}",
        detail=f"link_id={link.id};child_project_id={child.id}",
    )
    db.session.commit()
    flash("项目关联已建立；子项目侧不会显示父项目", "success")
    return redirect(url_for("main.project_links_admin"))


@bp.post("/admin/project-links/<int:link_id>/end")
@login_required
def project_link_end(link_id: int):
    if not _require_super_admin():
        return redirect(url_for("main.dashboard"))
    link = db.session.get(ProjectLink, int(link_id))
    if link is None or link.status != "active":
        flash("关联不存在或已经结束", "warning")
        return redirect(url_for("main.project_links_admin"))
    link.status = "ended"
    link.end_date = date.today()
    link.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    _log_project_activity(
        int(link.parent_project_id),
        "project_link.end",
        f"结束隐藏关联：{link.child_project.name}",
        detail=f"link_id={link.id};child_project_id={link.child_project_id}",
    )
    db.session.commit()
    flash("项目关联已结束；子项目仍作为独立项目运行", "success")
    return redirect(url_for("main.project_links_admin"))
