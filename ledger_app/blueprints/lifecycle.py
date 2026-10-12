"""lifecycle.py：路由实现（原 routes.py 拆分而来，URL/行为不变）。"""
from ._common import *  # noqa: F401,F403



@bp.route("/projects/<int:project_id>/end-date-change", methods=["GET", "POST"])
@login_required
def project_end_date_change(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if not _check_project_not_ended(p):
        return redirect(url_for("main.project_detail", project_id=p.id))
    if not _is_project_member(p.id, current_user.id):
        flash("你不是该项目成员，无法发起/处理结束日期变更", "danger")
        return redirect(url_for("main.projects_list"))

    if _open_project_end_date_change_request(p.id):
        flash("已存在待审批的结束日期变更申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    form = ProjectEndDateChangeForm()
    if form.validate_on_submit():
        old_end = p.planned_end_date or date.today()
        new_end = form.new_end_date.data
        if new_end == old_end:
            flash("新结束日期与当前一致", "info")
            return redirect(url_for("main.project_detail", project_id=p.id))
        if new_end < (p.planned_start_date or old_end):
            flash("新结束日期不能早于计划开始日期", "warning")
            return redirect(url_for("main.project_detail", project_id=p.id))

        req = ProjectEndDateChangeRequest(
            project_id=p.id,
            status="open",
            old_end_date=old_end,
            new_end_date=new_end,
            created_by_user_id=current_user.id,
        )
        db.session.add(req)
        db.session.flush()
        db.session.add(ProjectEndDateChangeApproval(request_id=req.id, user_id=current_user.id))
        _log_project_activity(
            int(p.id),
            "project.end_date_request",
            f"发起计划结束日期变更：{old_end} → {new_end}",
        )
        db.session.flush()

        # admin 发起即自动执行
        if _project_end_date_change_can_execute(req):
            p.planned_end_date = req.new_end_date
            req.status = "executed"
            req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            req.executed_by_user_id = current_user.id
            _log_project_activity(
                int(p.id),
                "project.end_date_execute",
                f"结束日期变更自动执行：{req.old_end_date} → {req.new_end_date}",
            )
            db.session.commit()
            flash("结束日期变更申请已提交，条件满足已自动生效", "success")
        else:
            db.session.commit()
            flash("已提交结束日期变更申请，待项目成员全部同意后生效", "success")
        return redirect(url_for("main.project_detail", project_id=p.id))

    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/end-date-change-approve")
@login_required
def project_end_date_change_approve(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if not is_admin() and not _is_project_member(p.id, current_user.id):
        flash("你不是该项目成员，无法同意", "danger")
        return redirect(url_for("main.projects_list"))

    req = _open_project_end_date_change_request(p.id)
    if not req:
        flash("没有待审批的结束日期变更申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    exists = ProjectEndDateChangeApproval.query.filter_by(
        request_id=req.id, user_id=current_user.id
    ).first()
    if exists:
        flash("你已同意，无需重复操作", "info")
        return redirect(url_for("main.project_detail", project_id=p.id))

    db.session.add(ProjectEndDateChangeApproval(request_id=req.id, user_id=current_user.id))
    _log_project_activity(
        int(p.id),
        "project.end_date_approve",
        f"用户 {current_user.username} 同意结束日期变更（{req.old_end_date} → {req.new_end_date}）",
    )
    db.session.flush()

    # 自动执行：条件满足时直接生效
    if _project_end_date_change_can_execute(req):
        p.planned_end_date = req.new_end_date
        req.status = "executed"
        req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.executed_by_user_id = current_user.id
        _log_project_activity(
            int(p.id),
            "project.end_date_execute",
            f"结束日期变更自动执行：{req.old_end_date} → {req.new_end_date}",
        )
        db.session.commit()
        flash("结束日期变更已同意，条件满足已自动执行生效", "success")
    else:
        db.session.commit()
        flash("已同意结束日期变更，等待其他成员同意后自动生效", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/end-date-change-execute")
@login_required
def project_end_date_change_execute(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    req = _open_project_end_date_change_request(p.id)
    if not req:
        flash("没有待审批的结束日期变更申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    if not _project_end_date_change_can_execute(req):
        flash(
            "须全体项目成员同意，或至少一名管理员点击「同意变更」后，才能执行结束日期调整。",
            "warning",
        )
        return redirect(url_for("main.project_detail", project_id=p.id))

    p.planned_end_date = req.new_end_date
    req.status = "executed"
    req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    req.executed_by_user_id = current_user.id
    _log_project_activity(
        int(p.id),
        "project.end_date_execute",
        f"管理员执行计划结束日期变更：{req.old_end_date} → {req.new_end_date}",
    )
    db.session.commit()
    flash("结束日期已更新", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/end-date-change-cancel")
@login_required
def project_end_date_change_cancel(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    req = _open_project_end_date_change_request(p.id)
    if not req:
        flash("没有待审批的结束日期变更申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    if not (is_admin() or req.created_by_user_id == current_user.id):
        flash("无权限取消该申请", "danger")
        return redirect(url_for("main.project_detail", project_id=p.id))

    req.status = "cancelled"
    _log_project_activity(
        int(p.id),
        "project.end_date_cancel",
        f"取消结束日期变更申请（原拟 {req.old_end_date} → {req.new_end_date}）",
    )
    db.session.commit()
    flash("已取消结束日期变更申请", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/end-request")
@login_required
def project_end_request(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if p.status == "ended":
        flash("项目已终止", "info")
        return redirect(url_for("main.project_detail", project_id=p.id))

    if not _is_project_member(p.id, current_user.id):
        flash("你不是该项目成员，无法发起终止申请", "danger")
        return redirect(url_for("main.projects_list"))

    if _open_project_end_request(p.id):
        flash("已存在待审批的终止申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    req = ProjectEndRequest(
        project_id=p.id,
        status="open",
        created_by_user_id=current_user.id,
    )
    db.session.add(req)
    db.session.flush()
    db.session.add(ProjectEndApproval(request_id=req.id, user_id=current_user.id))
    _log_project_activity(
        int(p.id),
        "project.end_request",
        f"发起终止项目申请",
    )
    db.session.flush()

    # admin 发起即自动执行
    if _project_end_can_execute(req):
        p.status = "ended"
        p.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.status = "executed"
        req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.executed_by_user_id = current_user.id
        _log_project_activity(
            int(p.id),
            "project.end_execute",
            "项目已终止，已冻结为只读",
        )
        db.session.commit()
        flash("终止项目申请已提交，条件满足已自动执行，项目已冻结", "success")
    else:
        db.session.commit()
        flash("已提交终止项目申请，待审核通过后项目将冻结为只读", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/end-approve")
@login_required
def project_end_approve(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if not is_admin() and not _is_project_member(p.id, current_user.id):
        flash("你不是该项目成员，无法同意", "danger")
        return redirect(url_for("main.projects_list"))

    req = _open_project_end_request(p.id)
    if not req:
        flash("没有待审批的终止申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    exists = ProjectEndApproval.query.filter_by(
        request_id=req.id, user_id=current_user.id
    ).first()
    if exists:
        flash("你已同意，无需重复操作", "info")
        return redirect(url_for("main.project_detail", project_id=p.id))

    db.session.add(ProjectEndApproval(request_id=req.id, user_id=current_user.id))
    _log_project_activity(
        int(p.id),
        "project.end_approve",
        f"用户 {current_user.username} 同意终止项目",
    )
    db.session.flush()

    # 自动执行：条件满足时直接冻结项目
    if _project_end_can_execute(req):
        p.status = "ended"
        p.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.status = "executed"
        req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.executed_by_user_id = current_user.id
        _log_project_activity(
            int(p.id),
            "project.end_execute",
            "项目已终止，已冻结为只读",
        )
        db.session.commit()
        flash("终止项目已同意，条件满足已自动执行，项目已冻结", "success")
    else:
        db.session.commit()
        flash("已同意终止项目，等待其他成员同意后自动执行", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/end-cancel")
@login_required
def project_end_cancel(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    req = _open_project_end_request(p.id)
    if not req:
        flash("没有待审批的终止申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    if not (is_admin() or req.created_by_user_id == current_user.id):
        flash("无权限取消该申请", "danger")
        return redirect(url_for("main.project_detail", project_id=p.id))

    req.status = "cancelled"
    _log_project_activity(
        int(p.id),
        "project.end_cancel",
        "取消终止项目申请",
    )
    db.session.commit()
    flash("已取消终止项目申请", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/revive-request")
@login_required
def project_revive_request(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if p.status != "ended":
        flash("只有已终止的项目才能发起复活申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    if not _is_project_member(p.id, current_user.id):
        flash("你不是该项目成员，无法发起复活申请", "danger")
        return redirect(url_for("main.projects_list"))

    if _open_project_revive_request(p.id):
        flash("已存在待审批的复活申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    req = ProjectReviveRequest(
        project_id=p.id, status="open", created_by_user_id=current_user.id,
    )
    db.session.add(req)
    db.session.flush()
    db.session.add(ProjectReviveApproval(request_id=req.id, user_id=current_user.id))
    _log_project_activity(int(p.id), "project.revive_request", "发起复活项目申请")
    db.session.flush()

    if _project_revive_can_execute(req):
        p.status = "open"
        p.ended_at = None
        req.status = "executed"
        req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.executed_by_user_id = current_user.id
        _log_project_activity(int(p.id), "project.revive_execute", "项目已复活，恢复为进行中状态")
        db.session.commit()
        flash("复活申请已提交，条件满足已自动执行，项目已恢复", "success")
    else:
        db.session.commit()
        flash("已提交复活项目申请，待审核通过后项目将恢复为进行中", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/revive-approve")
@login_required
def project_revive_approve(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if not is_admin() and not _is_project_member(p.id, current_user.id):
        flash("你不是该项目成员，无法同意", "danger")
        return redirect(url_for("main.projects_list"))

    req = _open_project_revive_request(p.id)
    if not req:
        flash("没有待审批的复活申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    if ProjectReviveApproval.query.filter_by(request_id=req.id, user_id=current_user.id).first():
        flash("你已同意，无需重复操作", "info")
        return redirect(url_for("main.project_detail", project_id=p.id))

    db.session.add(ProjectReviveApproval(request_id=req.id, user_id=current_user.id))
    _log_project_activity(int(p.id), "project.revive_approve", f"用户 {current_user.username} 同意复活项目")
    db.session.flush()

    if _project_revive_can_execute(req):
        p.status = "open"
        p.ended_at = None
        req.status = "executed"
        req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.executed_by_user_id = current_user.id
        _log_project_activity(int(p.id), "project.revive_execute", "项目已复活，恢复为进行中状态")
        db.session.commit()
        flash("复活项目已同意，条件满足已自动执行，项目已恢复", "success")
    else:
        db.session.commit()
        flash("已同意复活项目，等待其他成员同意后自动执行", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/revive-cancel")
@login_required
def project_revive_cancel(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    req = _open_project_revive_request(p.id)
    if not req:
        flash("没有待审批的复活申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    if not (is_admin() or req.created_by_user_id == current_user.id):
        flash("无权限取消该申请", "danger")
        return redirect(url_for("main.project_detail", project_id=p.id))

    req.status = "cancelled"
    _log_project_activity(int(p.id), "project.revive_cancel", "取消复活项目申请")
    db.session.commit()
    flash("已取消复活项目申请", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/delete-request")
@login_required
def projects_delete_request(project_id: int):
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.project_detail", project_id=project_id))

    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if not _check_project_not_ended(p):
        return redirect(url_for("main.project_detail", project_id=project_id))

    # 禁止删除有未终止子项目的父项目
    if p.sub_projects:
        active_subs = [sub for sub in p.sub_projects if sub.status != "ended"]
        if active_subs:
            names = "、".join(sub.name for sub in active_subs)
            flash(f"该项目有未终止的子项目（{names}），请先终止所有子项目后再删除父项目", "warning")
            return redirect(url_for("main.project_detail", project_id=project_id))

    existing = ProjectDeleteRequest.query.filter_by(project_id=p.id, status="open").first()
    if existing:
        flash("已存在待处理的删除申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    req = ProjectDeleteRequest(
        project_id=p.id, created_by_user_id=current_user.id, status="open"
    )
    db.session.add(req)
    db.session.flush()
    db.session.add(ProjectDeleteApproval(request_id=req.id, user_id=current_user.id))
    _log_project_activity(
        int(p.id),
        "project.delete_request",
        "发起「删除项目」申请",
    )
    db.session.flush()

    # admin 发起即自动执行删除
    non_admin_ids = _project_non_admin_member_user_ids(int(p.id))
    approved_ids = {a.user_id for a in req.approvals}
    can_exec = (
        is_admin()
        or (bool(non_admin_ids) and non_admin_ids.issubset(approved_ids))
        or _approval_user_ids_include_any_admin(approved_ids)
    )
    if can_exec:
        pname = p.name
        pid = int(p.id)
        _log_project_activity(
            pid,
            "project.delete_execute",
            f"管理员执行删除项目「{pname}」（项目 ID {pid}）",
        )
        _do_delete_project(p)
        db.session.commit()
        flash("删除项目已发起，条件满足已自动执行", "success")
        return redirect(url_for("main.projects_list"))

    db.session.commit()
    flash("已发起删除申请，待项目成员全部同意", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/delete-approve")
@login_required
def projects_delete_approve(project_id: int):
    if not is_admin() and not _is_project_member(project_id, current_user.id):
        flash("你不是该项目成员，无法同意删除", "danger")
        return redirect(url_for("main.projects_list"))

    req = ProjectDeleteRequest.query.filter_by(project_id=project_id, status="open").first()
    if not req:
        flash("没有待处理的删除申请", "warning")
        return redirect(url_for("main.project_detail", project_id=project_id))

    exists = ProjectDeleteApproval.query.filter_by(
        request_id=req.id, user_id=current_user.id
    ).first()
    if exists:
        flash("你已同意，无需重复操作", "info")
        return redirect(url_for("main.project_detail", project_id=project_id))

    db.session.add(ProjectDeleteApproval(request_id=req.id, user_id=current_user.id))
    _log_project_activity(
        int(project_id),
        "project.delete_approve",
        f"用户 {current_user.username} 同意删除项目",
    )
    db.session.flush()

    # 自动执行：条件满足时直接删除，无需再点「执行删除」
    p = db.session.get(Project, project_id)
    non_admin_ids = _project_non_admin_member_user_ids(int(project_id))
    approved_ids = {a.user_id for a in req.approvals}
    can_exec = (
        is_admin()
        or (bool(non_admin_ids) and non_admin_ids.issubset(approved_ids))
        or _approval_user_ids_include_any_admin(approved_ids)
    )
    if p and can_exec:
        # 手动执行与 projects_delete_execute 相同的清理逻辑（不调用路由避免重定向）
        pname = p.name
        pid = int(p.id)
        _log_project_activity(
            pid,
            "project.delete_execute",
            f"管理员执行删除项目「{pname}」（项目 ID {pid}）",
        )
        _do_delete_project(p)
        db.session.commit()
        flash("删除项目已同意，条件满足已自动执行", "success")
        return redirect(url_for("main.projects_list"))
    else:
        db.session.commit()
        flash("已同意删除，等待其他成员同意后自动执行", "success")
    return redirect(url_for("main.project_detail", project_id=project_id))


def _do_delete_project(p: Project) -> None:
    """执行项目删除的完整清理逻辑（不 commit，由调用方 commit）。"""
    pid = int(p.id)
    upload_root = current_app.config["UPLOAD_FOLDER"]

    # 记录日志（调用方应已在之前记录）

    txs = Transaction.query.filter_by(project_id=pid).all()
    tx_ids = [t.id for t in txs]
    members = ProjectMember.query.filter_by(project_id=pid).all()

    # Delete transaction edit workflow first
    if tx_ids:
        edit_req_ids = [
            int(r[0])
            for r in db.session.query(TransactionEditRequest.id)
            .filter(TransactionEditRequest.transaction_id.in_(tx_ids))
            .all()
        ]
        if edit_req_ids:
            db.session.query(TransactionEditApproval).filter(
                TransactionEditApproval.request_id.in_(edit_req_ids)
            ).delete(synchronize_session=False)
            db.session.query(TransactionEditRequest).filter(
                TransactionEditRequest.id.in_(edit_req_ids)
            ).delete(synchronize_session=False)

        proj_edit_req_ids = [
            int(r[0])
            for r in db.session.query(TransactionEditRequest.id)
            .filter(TransactionEditRequest.project_id == pid)
            .all()
        ]
        if proj_edit_req_ids:
            db.session.query(TransactionEditApproval).filter(
                TransactionEditApproval.request_id.in_(proj_edit_req_ids)
            ).delete(synchronize_session=False)
            db.session.query(TransactionEditRequest).filter(
                TransactionEditRequest.id.in_(proj_edit_req_ids)
            ).delete(synchronize_session=False)

    # Delete project end-date change workflow
    end_req_ids = [
        int(r[0])
        for r in db.session.query(ProjectEndDateChangeRequest.id)
        .filter(ProjectEndDateChangeRequest.project_id == pid)
        .all()
    ]
    if end_req_ids:
        db.session.query(ProjectEndDateChangeApproval).filter(
            ProjectEndDateChangeApproval.request_id.in_(end_req_ids)
        ).delete(synchronize_session=False)
        db.session.query(ProjectEndDateChangeRequest).filter(
            ProjectEndDateChangeRequest.id.in_(end_req_ids)
        ).delete(synchronize_session=False)

    # Delete project updates + attachments (files)
    updates = ProjectUpdate.query.filter_by(project_id=pid).all()
    for urow in updates:
        uatts = ProjectUpdateAttachment.query.filter_by(update_id=urow.id).all()
        for ua in uatts:
            try:
                (Path(upload_root) / ua.stored_path).unlink(missing_ok=True)
            except Exception:
                pass
            db.session.delete(ua)
        db.session.delete(urow)

    db.session.query(ProjectDividendDistribution).filter(
        ProjectDividendDistribution.project_id == pid
    ).delete(synchronize_session=False)

    for t in txs:
        atts = Attachment.query.filter_by(transaction_id=t.id).all()
        for a in atts:
            try:
                (Path(upload_root) / a.stored_path).unlink(missing_ok=True)
            except Exception:
                pass
            db.session.delete(a)
        db.session.delete(t)

    adjs = ProjectExpectedIncomeAdjustment.query.filter_by(project_id=pid).all()
    for a in adjs:
        db.session.delete(a)

    for m in members:
        db.session.delete(m)

    # Clean up project delete workflow records
    proj_del_req_ids = [
        int(r[0])
        for r in db.session.query(ProjectDeleteRequest.id)
        .filter(ProjectDeleteRequest.project_id == pid)
        .all()
    ]
    if proj_del_req_ids:
        db.session.query(ProjectDeleteApproval).filter(
            ProjectDeleteApproval.request_id.in_(proj_del_req_ids)
        ).delete(synchronize_session=False)
        db.session.query(ProjectDeleteRequest).filter(
            ProjectDeleteRequest.id.in_(proj_del_req_ids)
        ).delete(synchronize_session=False)

    # Clean up project end/revive workflow records
    for model_cls, approval_cls in [
        (ProjectEndRequest, ProjectEndApproval),
        (ProjectReviveRequest, ProjectReviveApproval),
    ]:
        req_ids = [int(r[0]) for r in db.session.query(model_cls.id).filter(model_cls.project_id == pid).all()]
        if req_ids:
            db.session.query(approval_cls).filter(approval_cls.request_id.in_(req_ids)).delete(synchronize_session=False)
            db.session.query(model_cls).filter(model_cls.id.in_(req_ids)).delete(synchronize_session=False)

    # Clean up adjustment approvals before deleting adjustments
    adj_ids = [int(r[0]) for r in db.session.query(ProjectExpectedIncomeAdjustment.id).filter(ProjectExpectedIncomeAdjustment.project_id == pid).all()]
    if adj_ids:
        db.session.query(ProjectAdjustmentApproval).filter(
            ProjectAdjustmentApproval.adjustment_id.in_(adj_ids)
        ).delete(synchronize_session=False)

    # Clean up dividend recipients
    db.session.query(ProjectDividendRecipient).filter(
        ProjectDividendRecipient.project_id == pid
    ).delete(synchronize_session=False)

    db.session.delete(p)


@bp.post("/projects/<int:project_id>/delete-execute")
@login_required
def projects_delete_execute(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if not _check_project_not_ended(p):
        return redirect(url_for("main.project_detail", project_id=p.id))

    req = ProjectDeleteRequest.query.filter_by(project_id=p.id, status="open").first()
    if not req:
        flash("没有待处理的删除申请", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    members = ProjectMember.query.filter_by(project_id=p.id).all()
    member_ids = {m.user_id for m in members}
    # 非 admin 成员必须全部同意，才视为"全员就绪"
    non_admin_ids = _project_non_admin_member_user_ids(int(p.id))
    approved_ids = {a.user_id for a in req.approvals}
    if (
        not is_admin()
        and not (bool(non_admin_ids) and non_admin_ids.issubset(approved_ids))
        and not _approval_user_ids_include_any_admin(approved_ids)
    ):
        flash(
            "须项目非管理员成员全部同意删除，或至少一名管理员在「同意删除」中确认后，才能执行删除。",
            "warning",
        )
        return redirect(url_for("main.project_detail", project_id=p.id))

    pname = p.name
    pid = int(p.id)
    _log_project_activity(
        pid,
        "project.delete_execute",
        f"管理员执行删除项目「{pname}」（项目 ID {pid}）",
    )
    _do_delete_project(p)
    db.session.commit()
    flash("项目已删除", "success")
    return redirect(url_for("main.projects_list"))


@bp.route("/projects/<int:project_id>/adjust", methods=["GET", "POST"])
@login_required
def project_adjust(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if not _check_project_not_ended(p):
        return redirect(url_for("main.project_detail", project_id=project_id))
    if not is_admin() and not _is_project_member(p.id, current_user.id):
        flash("你不是该项目成员，无权限", "danger")
        return redirect(url_for("main.project_detail", project_id=project_id))

    form = ProjectAdjustmentForm()
    if form.validate_on_submit():
        status = "active" if is_admin() else "pending"
        adj = ProjectExpectedIncomeAdjustment(
            project_id=p.id,
            amount_cents=yuan_to_cents(form.amount_yuan.data),
            note=form.note.data or None,
            status=status,
            created_by_user_id=current_user.id,
        )
        db.session.add(adj)
        db.session.flush()

        if status == "pending":
            # 创建者自动同意
            db.session.add(ProjectAdjustmentApproval(
                adjustment_id=adj.id,
                user_id=current_user.id,
            ))
            _log_project_activity(
                int(p.id),
                "project.adjust_request",
                f"发起追加应收 ¥{format_cents(int(adj.amount_cents))} 申请"
                + (f"（{adj.note}）" if adj.note else ""),
                detail=f"adjustment_id={adj.id}",
            )
            db.session.commit()
            flash("已提交追加应收申请，待审核通过后生效", "success")
        else:
            _log_project_activity(
                int(p.id),
                "project.adjust_expected",
                f"追加应收 ¥{format_cents(int(adj.amount_cents))}"
                + (f"（{adj.note}）" if adj.note else ""),
                detail=f"adjustment_id={adj.id}",
            )
            db.session.commit()
            flash("已追加应收款项", "success")
        return redirect(url_for("main.project_detail", project_id=p.id))

    return render_template("project_adjust.html", project=p, form=form, is_admin=is_admin())


@bp.post("/projects/<int:project_id>/adjust-approve/<int:adjustment_id>")
@login_required
def project_adjust_approve(project_id: int, adjustment_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))
    if not is_admin() and not _is_project_member(p.id, current_user.id):
        flash("你不是该项目成员", "danger")
        return redirect(url_for("main.projects_list"))

    adj = db.session.get(ProjectExpectedIncomeAdjustment, adjustment_id)
    if not adj or int(adj.project_id) != int(p.id) or adj.status != "pending":
        flash("该追加申请不存在或已处理", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    exists = ProjectAdjustmentApproval.query.filter_by(
        adjustment_id=adj.id, user_id=current_user.id
    ).first()
    if exists:
        flash("你已同意，无需重复操作", "info")
        return redirect(url_for("main.project_detail", project_id=p.id))

    db.session.add(ProjectAdjustmentApproval(adjustment_id=adj.id, user_id=current_user.id))
    db.session.flush()

    # 自动执行
    if _adjustment_can_execute(adj):
        adj.status = "executed"
        _log_project_activity(
            int(p.id),
            "project.adjust_execute",
            f"追加应收自动执行：¥{format_cents(int(adj.amount_cents))}"
            + (f"（{adj.note}）" if adj.note else ""),
        )
        db.session.commit()
        flash("追加应收已同意，条件满足已自动执行", "success")
    else:
        db.session.commit()
        flash("已同意追加应收，等待其他成员同意后自动执行", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/adjust-cancel/<int:adjustment_id>")
@login_required
def project_adjust_cancel(project_id: int, adjustment_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    adj = db.session.get(ProjectExpectedIncomeAdjustment, adjustment_id)
    if not adj or int(adj.project_id) != int(p.id) or adj.status != "pending":
        flash("该追加申请不存在或已处理", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    if not (is_admin() or adj.created_by_user_id == current_user.id):
        flash("无权限取消该申请", "danger")
        return redirect(url_for("main.project_detail", project_id=p.id))

    adj.status = "cancelled"
    _log_project_activity(
        int(p.id),
        "project.adjust_cancel",
        f"取消追加应收 ¥{format_cents(int(adj.amount_cents))} 申请",
    )
    db.session.commit()
    flash("已取消追加应收申请", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))

