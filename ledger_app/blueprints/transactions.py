"""transactions.py：路由实现（原 routes.py 拆分而来，URL/行为不变）。"""
from ._common import *  # noqa: F401,F403



@bp.get("/transactions")
@login_required
def transactions_list():
    q = Transaction.query.filter(
        Transaction.is_void.is_(False),
        Transaction.status == "active",
        Transaction.project_id.isnot(None),
    )
    q = _apply_transaction_project_access_filter(q)
    project_id = request.args.get("project_id", type=int)
    if project_id:
        if not is_admin() and not _is_project_member(project_id, current_user.id):
            flash("你不是该项目成员，无权按该项目筛选。", "danger")
            return redirect(url_for("main.transactions_list"))
        q = q.filter(Transaction.project_id == project_id)
    page = request.args.get("page", 1, type=int) or 1
    page = max(page, 1)
    pagination = (
        q.options(joinedload(Transaction.project), joinedload(Transaction.attachments))
        .order_by(Transaction.occur_date.desc(), Transaction.id.desc())
        .paginate(page=page, per_page=25, error_out=False)
    )
    txs = pagination.items
    projects = _accessible_projects_query().order_by(Project.name.asc()).all()

    edit_req_by_tx = {}
    edit_ready_by_tx = {}
    member_count_by_project = {}
    if txs:
        ids = [t.id for t in txs]
        reqs = TransactionEditRequest.query.filter(
            TransactionEditRequest.status == "open",
            TransactionEditRequest.transaction_id.in_(ids),
        ).all()
        edit_req_by_tx = {r.transaction_id: r for r in reqs}
        edit_ready_by_tx = {tid: _transaction_edit_fully_approved(r) for tid, r in edit_req_by_tx.items()}

        pids = sorted({int(t.project_id) for t in txs if t.project_id})
        if pids:
            rows = (
                db.session.query(ProjectMember.project_id, db.func.count(ProjectMember.user_id))
                .filter(ProjectMember.project_id.in_(pids))
                .group_by(ProjectMember.project_id)
                .all()
            )
            member_count_by_project = {int(pid): int(c) for pid, c in rows}

    return render_template(
        "transactions_list.html",
        transactions=txs,
        pagination=pagination,
        edit_req_by_tx=edit_req_by_tx,
        edit_ready_by_tx=edit_ready_by_tx,
        member_count_by_project=member_count_by_project,
        projects=projects,
        current_project_id=project_id,
        is_admin=is_admin(),
    )


@bp.route("/transactions/new", methods=["GET", "POST"])
@login_required
def transactions_new():
    form = TransactionForm()
    projects = _accessible_projects_query().filter(Project.status != "ended").order_by(Project.name.asc()).all()
    if not projects:
        flash("暂无可登记流水的项目：请先加入某项目或由管理员创建项目。", "warning")
        return redirect(url_for("main.projects_list"))
    form.project_id.choices = [(p.id, p.name) for p in projects]
    allowed_ids = {int(p.id) for p in projects}
    default_pid = request.args.get("project_id", type=int)
    if request.method == "GET":
        if default_pid and default_pid in allowed_ids:
            form.project_id.data = int(default_pid)
        else:
            form.project_id.data = int(projects[0].id)
    sel_pid = form.project_id.data or default_pid or (projects[0].id if projects else None)
    _set_counterparty_choices(form, sel_pid)
    if form.validate_on_submit():
        pid = int(form.project_id.data)
        if pid not in allowed_ids:
            flash("所选项目不在你的可访问范围内。", "danger")
            return render_template("transaction_form.html", form=form, is_admin=is_admin())
        if not is_admin() and not _is_project_member(pid, current_user.id):
            flash("你不是该项目成员，不能在该项目下登记流水。", "danger")
            return render_template("transaction_form.html", form=form, is_admin=is_admin())
        p = db.session.get(Project, pid)
        if not _check_project_not_ended(p):
            return render_template("transaction_form.html", form=form, is_admin=is_admin())
        if not db.session.get(Project, pid):
            flash("所选项目不存在", "warning")
            return render_template("transaction_form.html", form=form, is_admin=is_admin())
        tx_status = "active" if is_admin() else "pending"
        tx = Transaction(
            project_id=pid, status=tx_status, type=form.type.data,
            amount_cents=yuan_to_cents(form.amount_yuan.data),
            occur_date=form.occur_date.data, settled=bool(form.settled.data),
            counterparty=form.counterparty.data or None,
            recipient_user_id=form.recipient_user_id.data if form.counterparty.data == CP_EXPENSE_MEMBER else None,
            note=form.note.data or None, created_by_user_id=current_user.id,
        )
        db.session.add(tx)
        db.session.flush()
        added = _save_transaction_attachments(tx)
        typ = "收入" if tx.type == TransactionType.INCOME.value else "支出"
        if tx_status == "pending":
            creq = TransactionCreateRequest(transaction_id=tx.id, project_id=pid, status="open", created_by_user_id=current_user.id)
            db.session.add(creq)
            db.session.flush()
            db.session.add(TransactionCreateApproval(request_id=creq.id, user_id=current_user.id))
            _log_project_activity(pid, "transaction.create_request",
                f"发起新增流水 #{tx.id} 审批：{typ} ¥{format_cents(int(tx.amount_cents))}，日期 {tx.occur_date}"
                + (f"，附件 {added} 个" if added else ""), detail=f"request_id={creq.id}")
        else:
            _log_project_activity(pid, "transaction.create",
                f"新增流水 #{tx.id}：{typ} ¥{format_cents(int(tx.amount_cents))}，日期 {tx.occur_date}"
                + (f"，附件 {added} 个" if added else ""))
        db.session.commit()
        if tx_status == "pending":
            flash("已提交新增流水申请，待审核通过后生效。", "success")
        else:
            flash(f"流水已新增{'，已保存 ' + str(added) + ' 个凭证文件' if added else ''}", "success")
        return redirect(url_for("main.project_detail", project_id=pid))
    return render_template("transaction_form.html", form=form, is_admin=is_admin())


@bp.route("/transactions/<int:transaction_id>/edit", methods=["GET", "POST"])
@login_required
def transactions_edit(transaction_id: int):
    tx = (
        db.session.query(Transaction)
        .options(joinedload(Transaction.attachments))
        .filter_by(id=transaction_id)
        .first()
    )
    if not tx or tx.is_void:
        flash("流水不存在", "warning")
        return redirect(url_for("main.transactions_list"))

    p = db.session.get(Project, int(tx.project_id))
    if not _check_project_not_ended(p):
        return redirect(url_for("main.project_detail", project_id=int(tx.project_id)))

    if not _is_project_member(int(tx.project_id), current_user.id):
        flash("你不是该项目成员：请先在项目成员里添加你的账号，再发起修改申请", "warning")
        return redirect(url_for("main.project_detail", project_id=int(tx.project_id)))

    if _open_transaction_edit_request(tx.id):
        flash("该流水已有待审批的修改申请", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    form = TransactionEditForm()
    # 设置对方选项（含当前值，保证旧数据可编辑）
    _set_counterparty_choices(form, int(tx.project_id))
    old_cp = tx.counterparty or ""
    if old_cp and old_cp not in [c[0] for c in form.counterparty.choices]:
        form.counterparty.choices = [(old_cp, f"{old_cp}（原值）")] + form.counterparty.choices
    form.recipient_user_id.choices = [(0, "（不指定）")] + [
        (u.id, u.username) for u in
        db.session.query(User).join(ProjectMember, ProjectMember.user_id == User.id)
        .filter(ProjectMember.project_id == int(tx.project_id))
        .order_by(User.username.asc()).all()
    ]
    if request.method == "GET":
        form.type.data = tx.type
        form.amount_yuan.data = cents_to_yuan(int(tx.amount_cents))
        form.occur_date.data = tx.occur_date
        form.settled.data = bool(tx.settled)
        form.counterparty.data = old_cp
        form.recipient_user_id.data = tx.recipient_user_id or 0
        form.note.data = tx.note or ""

    if form.validate_on_submit():
        # 保护分红流水标记：编辑时自动保留 [DIVIDEND] 前缀
        raw_note = form.note.data or None
        if raw_note and (tx.note or "").startswith("[DIVIDEND]") and not raw_note.startswith("[DIVIDEND]"):
            raw_note = f"[DIVIDEND] {raw_note}"
        req = TransactionEditRequest(
            transaction_id=tx.id,
            project_id=int(tx.project_id),
            status="open",
            new_type=form.type.data,
            new_amount_cents=yuan_to_cents(form.amount_yuan.data),
            new_occur_date=form.occur_date.data,
            new_settled=bool(form.settled.data),
            new_counterparty=form.counterparty.data or None,
            new_recipient_user_id=form.recipient_user_id.data if form.counterparty.data == CP_EXPENSE_MEMBER else None,
            new_note=raw_note,
            created_by_user_id=current_user.id,
        )
        db.session.add(req)
        db.session.flush()
        db.session.add(TransactionEditApproval(request_id=req.id, user_id=current_user.id))
        removed = _apply_transaction_attachment_deletes(
            tx, request.form.getlist("delete_attachment_ids")
        )
        added = _save_transaction_attachments(tx)
        ntyp = "收入" if req.new_type == TransactionType.INCOME.value else "支出"
        _log_project_activity(
            int(tx.project_id),
            "transaction.edit_request",
            f"发起流水 #{tx.id} 修改申请 → {ntyp} ¥{format_cents(int(req.new_amount_cents))}，{req.new_occur_date}"
            + (f"；追加凭证 {added} 个" if added else "")
            + (f"；移除凭证 {removed} 个" if removed else ""),
            detail=f"request_id={req.id}",
        )
        db.session.flush()

        # admin 发起即自动执行修改
        if _transaction_edit_can_execute(req):
            tx.type = req.new_type
            tx.amount_cents = int(req.new_amount_cents)
            tx.occur_date = req.new_occur_date
            tx.settled = bool(req.new_settled)
            tx.counterparty = req.new_counterparty
            tx.recipient_user_id = req.new_recipient_user_id
            tx.note = req.new_note
            req.status = "executed"
            req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            req.executed_by_user_id = current_user.id
            _log_project_activity(
                int(tx.project_id),
                "transaction.edit_execute",
                f"修改自动执行（流水 #{tx.id}）：{ntyp} ¥{format_cents(int(req.new_amount_cents))}",
                detail=f"request_id={req.id}",
            )
            msg = "修改申请已提交，条件满足已自动执行生效"
            if added:
                msg += f"，已追加 {added} 个凭证"
            if removed:
                msg += f"，已移除 {removed} 个凭证"
            db.session.commit()
            flash(msg, "success")
        else:
            msg = "已提交流水修改申请，待项目成员全部同意后生效。"
            if added:
                msg += f" 已追加 {added} 个凭证。"
            if removed:
                msg += f" 已移除 {removed} 个凭证。"
            db.session.commit()
            flash(msg, "success")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    p = db.session.get(Project, int(tx.project_id))
    return render_template(
        "transaction_edit.html",
        form=form,
        transaction=tx,
        project=p,
        is_admin=is_admin(),
    )


@bp.post("/transactions/<int:transaction_id>/edit-approve")
@login_required
def transactions_edit_approve(transaction_id: int):
    tx = db.session.get(Transaction, transaction_id)
    if not tx or tx.is_void or not tx.project_id:
        flash("流水不存在", "warning")
        return redirect(url_for("main.transactions_list"))

    if not is_admin() and not _is_project_member(int(tx.project_id), current_user.id):
        flash("你不是该项目成员，无法同意修改", "danger")
        return redirect(url_for("main.projects_list"))

    req = _open_transaction_edit_request(tx.id)
    if not req:
        flash("没有待审批的修改申请", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    exists = TransactionEditApproval.query.filter_by(
        request_id=req.id, user_id=current_user.id
    ).first()
    if exists:
        flash("你已同意，无需重复操作", "info")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    db.session.add(TransactionEditApproval(request_id=req.id, user_id=current_user.id))
    _log_project_activity(
        int(tx.project_id),
        "transaction.edit_approve",
        f"用户 {current_user.username} 同意流水 #{tx.id} 的修改申请",
        detail=f"request_id={req.id}",
    )
    db.session.flush()

    # 自动执行：条件满足时直接生效，无需再点「执行修改」
    if _transaction_edit_can_execute(req):
        tx.type = req.new_type
        tx.amount_cents = int(req.new_amount_cents)
        tx.occur_date = req.new_occur_date
        tx.settled = bool(req.new_settled)
        tx.counterparty = req.new_counterparty
        tx.recipient_user_id = req.new_recipient_user_id
        tx.note = req.new_note
        req.status = "executed"
        req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.executed_by_user_id = current_user.id
        ntyp = "收入" if req.new_type == TransactionType.INCOME.value else "支出"
        _log_project_activity(
            int(tx.project_id),
            "transaction.edit_execute",
            f"修改自动执行（流水 #{tx.id}）：{ntyp} ¥{format_cents(int(req.new_amount_cents))}，{req.new_occur_date}",
            detail=f"request_id={req.id}",
        )
        db.session.commit()
        flash("修改申请已同意，条件满足已自动执行生效", "success")
    else:
        db.session.commit()
        flash("已同意修改，等待其他成员同意后自动生效", "success")
    return redirect(url_for("main.project_detail", project_id=tx.project_id))


@bp.post("/transactions/<int:transaction_id>/edit-execute")
@login_required
def transactions_edit_execute(transaction_id: int):
    tx = db.session.get(Transaction, transaction_id)
    if not tx or tx.is_void or not tx.project_id:
        flash("流水不存在", "warning")
        return redirect(url_for("main.transactions_list"))

    if not (is_admin() or _is_project_member(int(tx.project_id), current_user.id)):
        flash("你不是该项目成员，无法执行修改", "danger")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    req = _open_transaction_edit_request(tx.id)
    if not req:
        flash("没有待审批的修改申请", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    if not _transaction_edit_can_execute(req):
        flash(
            "须全体项目成员同意，或至少一名管理员点击「同意修改」后，才能执行修改。",
            "warning",
        )
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    tx.type = req.new_type
    tx.amount_cents = int(req.new_amount_cents)
    tx.occur_date = req.new_occur_date
    tx.settled = bool(req.new_settled)
    tx.counterparty = req.new_counterparty
    tx.note = req.new_note

    req.status = "executed"
    req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    req.executed_by_user_id = current_user.id

    ntyp = "收入" if req.new_type == TransactionType.INCOME.value else "支出"
    _log_project_activity(
        int(tx.project_id),
        "transaction.edit_execute",
        f"管理员执行流水 #{tx.id} 修改：{ntyp} ¥{format_cents(int(req.new_amount_cents))}，{req.new_occur_date}",
        detail=f"request_id={req.id}",
    )
    db.session.commit()
    flash("流水修改已生效", "success")
    return redirect(url_for("main.project_detail", project_id=tx.project_id))


@bp.post("/transactions/<int:transaction_id>/edit-cancel")
@login_required
def transactions_edit_cancel(transaction_id: int):
    tx = db.session.get(Transaction, transaction_id)
    if not tx or tx.is_void or not tx.project_id:
        flash("流水不存在", "warning")
        return redirect(url_for("main.transactions_list"))

    req = _open_transaction_edit_request(tx.id)
    if not req:
        flash("没有待审批的修改申请", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    if not (is_admin() or req.created_by_user_id == current_user.id):
        flash("无权限取消该申请", "danger")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    req.status = "cancelled"
    _log_project_activity(
        int(tx.project_id),
        "transaction.edit_cancel",
        f"取消流水 #{tx.id} 的修改申请",
        detail=f"request_id={req.id}",
    )
    db.session.commit()
    flash("已取消流水修改申请", "success")
    return redirect(url_for("main.project_detail", project_id=tx.project_id))


@bp.post("/transactions/<int:transaction_id>/delete-request")
@login_required
def transactions_delete_request(transaction_id: int):
    tx = db.session.get(Transaction, transaction_id)
    if not tx or tx.is_void or not tx.project_id:
        flash("流水不存在", "warning")
        return redirect(url_for("main.transactions_list"))
    if getattr(tx, "status", "active") != "active":
        flash("该流水尚未审核生效，无法删除。", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    if not (is_admin() or _is_project_member(int(tx.project_id), current_user.id)):
        flash("你不是该项目成员，无法发起删除申请", "danger")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    p = db.session.get(Project, int(tx.project_id))
    if not _check_project_not_ended(p):
        return redirect(url_for("main.project_detail", project_id=int(tx.project_id)))

    if _open_transaction_delete_request(tx.id):
        flash("该流水已有待审批的删除申请", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    req = TransactionDeleteRequest(
        transaction_id=tx.id,
        project_id=int(tx.project_id),
        status="open",
        created_by_user_id=current_user.id,
    )
    db.session.add(req)
    db.session.flush()
    db.session.add(TransactionDeleteApproval(request_id=req.id, user_id=current_user.id))
    _log_project_activity(
        int(tx.project_id),
        "transaction.delete_request",
        f"发起流水 #{tx.id} 删除申请",
        detail=f"request_id={req.id}",
    )
    db.session.flush()

    # admin 发起即自动执行
    if _transaction_delete_can_execute(req):
        tx.is_void = True
        req.status = "executed"
        req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.executed_by_user_id = current_user.id
        _log_project_activity(
            int(tx.project_id),
            "transaction.delete_execute",
            f"删除自动执行（流水 #{tx.id}）",
            detail=f"request_id={req.id}",
        )
        db.session.commit()
        flash("删除申请已提交，条件满足已自动执行", "success")
    else:
        db.session.commit()
        flash("已提交删除申请，待审核通过后可执行删除。", "success")
    return redirect(url_for("main.project_detail", project_id=tx.project_id))


@bp.post("/transactions/<int:transaction_id>/delete-approve")
@login_required
def transactions_delete_approve(transaction_id: int):
    tx = db.session.get(Transaction, transaction_id)
    if not tx or tx.is_void or not tx.project_id:
        flash("流水不存在", "warning")
        return redirect(url_for("main.transactions_list"))

    if not (is_admin() or _is_project_member(int(tx.project_id), current_user.id)):
        flash("你不是该项目成员，无法同意删除", "danger")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    req = _open_transaction_delete_request(tx.id)
    if not req:
        flash("没有待审批的删除申请", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    exists = TransactionDeleteApproval.query.filter_by(
        request_id=req.id, user_id=current_user.id
    ).first()
    if exists:
        flash("你已同意，无需重复操作", "info")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    db.session.add(TransactionDeleteApproval(request_id=req.id, user_id=current_user.id))
    _log_project_activity(
        int(tx.project_id),
        "transaction.delete_approve",
        f"用户 {current_user.username} 同意删除流水 #{tx.id}",
        detail=f"request_id={req.id}",
    )
    db.session.flush()

    # 自动执行：条件满足时直接作废，无需再点「执行删除」
    if _transaction_delete_can_execute(req):
        tx.is_void = True
        req.status = "executed"
        req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.executed_by_user_id = current_user.id
        _log_project_activity(
            int(tx.project_id),
            "transaction.delete_execute",
            f"删除自动执行（流水 #{tx.id}）",
            detail=f"request_id={req.id}",
        )
        db.session.commit()
        flash("删除申请已同意，条件满足已自动执行", "success")
    else:
        db.session.commit()
        flash("已同意删除，等待其他成员同意后自动生效", "success")
    return redirect(url_for("main.project_detail", project_id=tx.project_id))


@bp.post("/transactions/<int:transaction_id>/delete-execute")
@login_required
def transactions_delete_execute(transaction_id: int):
    tx = db.session.get(Transaction, transaction_id)
    if not tx or tx.is_void or not tx.project_id:
        flash("流水不存在", "warning")
        return redirect(url_for("main.transactions_list"))

    if not (is_admin() or _is_project_member(int(tx.project_id), current_user.id)):
        flash("你不是该项目成员，无法执行删除", "danger")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    req = _open_transaction_delete_request(tx.id)
    if not req:
        flash("没有待审批的删除申请", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    if not _transaction_delete_can_execute(req):
        flash("未审核通过：需全体成员同意或管理员同意后，才能执行删除。", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    tx.is_void = True
    req.status = "executed"
    req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    req.executed_by_user_id = current_user.id
    _log_project_activity(
        int(tx.project_id),
        "transaction.delete_execute",
        f"执行删除流水 #{tx.id}",
        detail=f"request_id={req.id}",
    )
    db.session.commit()
    flash("流水已删除（已作废）", "success")
    return redirect(url_for("main.project_detail", project_id=tx.project_id))


@bp.post("/transactions/<int:transaction_id>/delete-cancel")
@login_required
def transactions_delete_cancel(transaction_id: int):
    tx = db.session.get(Transaction, transaction_id)
    if not tx or tx.is_void or not tx.project_id:
        flash("流水不存在", "warning")
        return redirect(url_for("main.transactions_list"))

    req = _open_transaction_delete_request(tx.id)
    if not req:
        flash("没有待审批的删除申请", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    if not (is_admin() or req.created_by_user_id == current_user.id):
        flash("无权限取消该删除申请", "danger")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    req.status = "cancelled"
    _log_project_activity(
        int(tx.project_id),
        "transaction.delete_cancel",
        f"取消流水 #{tx.id} 的删除申请",
        detail=f"request_id={req.id}",
    )
    db.session.commit()
    flash("已取消删除申请", "success")
    return redirect(url_for("main.project_detail", project_id=tx.project_id))


@bp.post("/transactions/<int:transaction_id>/create-approve")
@login_required
def transactions_create_approve(transaction_id: int):
    tx = db.session.get(Transaction, transaction_id)
    if not tx or tx.is_void or not tx.project_id:
        flash("流水不存在", "warning")
        return redirect(url_for("main.transactions_list"))
    if getattr(tx, "status", "active") != "pending":
        flash("该流水不处于待审核状态", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    if not (is_admin() or _is_project_member(int(tx.project_id), current_user.id)):
        flash("你不是该项目成员，无法同意", "danger")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    req = _open_transaction_create_request(tx.id)
    if not req:
        flash("没有待审批的新增申请", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    exists = TransactionCreateApproval.query.filter_by(
        request_id=req.id, user_id=current_user.id
    ).first()
    if exists:
        flash("你已同意，无需重复操作", "info")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    db.session.add(TransactionCreateApproval(request_id=req.id, user_id=current_user.id))
    _log_project_activity(
        int(tx.project_id),
        "transaction.create_approve",
        f"用户 {current_user.username} 同意新增流水 #{tx.id}",
        detail=f"request_id={req.id}",
    )
    db.session.flush()

    # 自动执行：条件满足时直接生效，无需再点「执行生效」
    if _transaction_create_can_execute(req):
        tx.status = "active"
        req.status = "executed"
        req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.executed_by_user_id = current_user.id
        _log_project_activity(
            int(tx.project_id),
            "transaction.create_execute",
            f"新增自动生效（流水 #{tx.id}）",
            detail=f"request_id={req.id}",
        )
        db.session.commit()
        flash("新增申请已同意，条件满足已自动执行生效", "success")
    else:
        db.session.commit()
        flash("已同意新增，等待其他成员同意后自动生效", "success")
    return redirect(url_for("main.project_detail", project_id=tx.project_id))


@bp.post("/transactions/<int:transaction_id>/create-execute")
@login_required
def transactions_create_execute(transaction_id: int):
    tx = db.session.get(Transaction, transaction_id)
    if not tx or tx.is_void or not tx.project_id:
        flash("流水不存在", "warning")
        return redirect(url_for("main.transactions_list"))
    if getattr(tx, "status", "active") != "pending":
        flash("该流水不处于待审核状态", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    if not (is_admin() or _is_project_member(int(tx.project_id), current_user.id)):
        flash("你不是该项目成员，无法执行", "danger")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    req = _open_transaction_create_request(tx.id)
    if not req:
        flash("没有待审批的新增申请", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    if not _transaction_create_can_execute(req):
        flash("未审核通过：需全体成员同意或管理员同意后，才能执行生效。", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    tx.status = "active"
    req.status = "executed"
    req.executed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    req.executed_by_user_id = current_user.id
    _log_project_activity(
        int(tx.project_id),
        "transaction.create_execute",
        f"执行新增流水 #{tx.id} 生效",
        detail=f"request_id={req.id}",
    )
    db.session.commit()
    flash("新增流水已生效", "success")
    return redirect(url_for("main.project_detail", project_id=tx.project_id))


@bp.post("/transactions/<int:transaction_id>/create-cancel")
@login_required
def transactions_create_cancel(transaction_id: int):
    tx = db.session.get(Transaction, transaction_id)
    if not tx or tx.is_void or not tx.project_id:
        flash("流水不存在", "warning")
        return redirect(url_for("main.transactions_list"))
    if getattr(tx, "status", "active") != "pending":
        flash("该流水不处于待审核状态", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    req = _open_transaction_create_request(tx.id)
    if not req:
        flash("没有待审批的新增申请", "warning")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    if not (is_admin() or req.created_by_user_id == current_user.id):
        flash("无权限取消该新增申请", "danger")
        return redirect(url_for("main.project_detail", project_id=tx.project_id))

    req.status = "cancelled"
    # 取消新增：将该待审核流水作废，避免进入统计/列表
    tx.is_void = True
    _log_project_activity(
        int(tx.project_id),
        "transaction.create_cancel",
        f"取消新增流水 #{tx.id} 申请",
        detail=f"request_id={req.id}",
    )
    db.session.commit()
    flash("已取消新增申请", "success")
    return redirect(url_for("main.project_detail", project_id=tx.project_id))

