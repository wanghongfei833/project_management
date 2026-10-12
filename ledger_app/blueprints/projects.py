"""projects.py：路由实现（原 routes.py 拆分而来，URL/行为不变）。"""
from ._common import *  # noqa: F401,F403



@bp.get("/projects")
@login_required
def projects_list():
    return redirect(url_for("main.dashboard"))


@bp.route("/projects/new", methods=["GET", "POST"])
@login_required
def projects_new():
    form = ProjectForm()
    users = User.query.filter_by(is_active=True).order_by(User.username.asc()).all()
    form.member_user_ids.choices = [(u.id, u.username) for u in users]
    form.leader_user_id.choices = [(u.id, u.username) for u in users]
    # 父项目选择：只显示没有父项目的项目（独立项目或已是父项目）
    parent_candidates = Project.query.filter(Project.status != "ended").order_by(Project.name.asc()).all()
    form.parent_project_id.choices = [(0, "（无——独立项目）")] + [(p.id, p.name) for p in parent_candidates]
    # 构建父项目成员映射（用于前端JS分行显示）
    parent_members_map = {
        int(p.id): [int(pm.user_id) for pm in ProjectMember.query.filter_by(project_id=p.id).all()]
        for p in parent_candidates
    }
    admin_user_ids = _admin_user_ids()
    # 计算父项目成员 ID（用于模板显示分两行）
    def _get_parent_member_ids():
        pid = form.parent_project_id.data
        if pid and pid != 0:
            pp = db.session.get(Project, pid)
            if pp:
                return {int(pm.user_id) for pm in ProjectMember.query.filter_by(project_id=pp.id).all()}
        return set()
    parent_member_ids = _get_parent_member_ids()

    if form.validate_on_submit():
        if form.planned_end_date.data < form.planned_start_date.data:
            flash("计划结束日期不能早于开始日期", "warning")
            return render_template("project_form.html", form=form, is_admin=is_admin(), parent_candidates=parent_candidates, admin_user_ids=admin_user_ids, parent_member_ids=parent_member_ids)

        # 校验项目名称唯一性
        name = form.name.data.strip()
        existing = Project.query.filter_by(name=name).first()
        if existing:
            flash(f"项目名称「{name}」已存在，请换一个名称", "warning")
            return render_template("project_form.html", form=form, is_admin=is_admin(), parent_candidates=parent_candidates, admin_user_ids=admin_user_ids, parent_member_ids=parent_member_ids)

        ratio_percent = form.referral_ratio_percent.data
        ratio = Decimal("0")
        if ratio_percent is not None:
            ratio = (Decimal(str(ratio_percent)) / Decimal("100")).quantize(
                Decimal("0.000001")
            )
        mode = form.broker_fee_mode.data
        direction = form.broker_fee_direction.data
        fixed_yuan = form.broker_fixed_fee_yuan.data or Decimal("0")
        fixed_cents = yuan_to_cents(fixed_yuan) if fixed_yuan > 0 else 0
        if mode == BROKER_MODE_PERCENT:
            fixed_cents = 0
        if (
            mode == BROKER_MODE_PERCENT
            and direction == BROKER_DIR_NET_FROM_BROKER
            and ratio >= Decimal("1")
        ):
            flash("「中介从客户款中扣除」且按比例时，中介比例须小于 100%。", "warning")
            return render_template("project_form.html", form=form, is_admin=is_admin(), parent_candidates=parent_candidates, admin_user_ids=admin_user_ids, parent_member_ids=parent_member_ids)

        parent_id = form.parent_project_id.data
        if parent_id == 0:
            parent_id = None
        can_div = form.can_dividend.data == 1 if parent_id else True
        p = Project(
            name=name,
            leader_user_id=int(form.leader_user_id.data),
            planned_start_date=form.planned_start_date.data,
            planned_end_date=form.planned_end_date.data,
            expected_income_cents=yuan_to_cents(form.expected_income_yuan.data),
            referral_ratio=ratio,
            broker_fee_mode=mode,
            broker_fee_direction=direction,
            broker_fixed_fee_cents=int(fixed_cents),
            status="open",
            note=form.note.data or None,
            parent_project_id=parent_id,
            can_dividend=can_div,
        )
        # 表单可选「已结束」；统一走 open/ended 两种状态并维护 ended_at
        _apply_project_status(p, form.status.data)
        db.session.add(p)
        db.session.flush()

        member_ids = set(form.member_user_ids.data or [])
        member_ids.update(admin_user_ids)  # admin 必须在项目内
        member_ids.add(current_user.id)  # 创建人必须是成员
        member_ids.add(int(form.leader_user_id.data))  # 负责人必须是成员
        for uid in member_ids:
            db.session.add(ProjectMember(project_id=p.id, user_id=int(uid)))

        _log_project_activity(
            int(p.id),
            "project.create",
            f"创建项目「{p.name}」",
        )
        db.session.commit()
        flash("项目已创建", "success")
        return redirect(url_for("main.projects_list"))

    # default
    if request.method == "GET":
        form.referral_ratio_percent.data = Decimal("0")
        form.member_user_ids.data = sorted({current_user.id, *admin_user_ids})
        form.leader_user_id.data = current_user.id
        form.planned_start_date.data = date.today()
        form.planned_end_date.data = date.today()
        form.broker_fee_mode.data = BROKER_MODE_PERCENT
        form.broker_fee_direction.data = BROKER_DIR_WE_PAY
        form.can_dividend.data = 1

    parent_member_ids = set()
    if form.parent_project_id.data and form.parent_project_id.data != 0:
        pp = db.session.get(Project, form.parent_project_id.data)
        if pp:
            parent_member_ids = {int(pm.user_id) for pm in ProjectMember.query.filter_by(project_id=pp.id).all()}

    return render_template(
        "project_form.html",
        form=form,
        is_admin=is_admin(),
        mode="create",
        parent_candidates=parent_candidates,
        admin_user_ids=admin_user_ids,
        parent_member_ids=parent_member_ids,
        parent_members_map_json=json.dumps(parent_members_map, ensure_ascii=False),
    )


@bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def projects_edit(project_id: int):
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.project_detail", project_id=project_id))

    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    form = ProjectForm()
    users = User.query.filter_by(is_active=True).order_by(User.username.asc()).all()
    form.member_user_ids.choices = [(u.id, u.username) for u in users]
    form.leader_user_id.choices = [(u.id, u.username) for u in users]
    # 父项目选择：当前项目及其子项目不可选
    # 父项目候选：排除自身和所有子孙项目（防环形引用）
    sub_ids = _collect_all_sub_project_ids(p.id)
    sub_ids.add(p.id)
    parent_candidates = Project.query.filter(
        ~Project.id.in_(sub_ids), Project.status != "ended"
    ).order_by(Project.name.asc()).all()
    parent_members_map = {
        int(pp.id): [int(pm.user_id) for pm in ProjectMember.query.filter_by(project_id=pp.id).all()]
        for pp in parent_candidates
    }
    admin_user_ids = _admin_user_ids()
    # 设置父项目下拉选项（必须在 validate_on_submit 之前）
    form.parent_project_id.choices = [(0, "（无——独立项目）")] + [(pp.id, pp.name) for pp in parent_candidates]
    # 计算父项目成员 ID
    def _get_parent_member_ids():
        pid = form.parent_project_id.data or p.parent_project_id
        if pid and pid != 0:
            pp = db.session.get(Project, int(pid))
            if pp:
                return {int(pm.user_id) for pm in ProjectMember.query.filter_by(project_id=pp.id).all()}
        return set()
    parent_member_ids = _get_parent_member_ids()

    if request.method == "GET":
        form.name.data = p.name
        form.leader_user_id.data = int(p.leader_user_id) if p.leader_user_id else current_user.id
        form.planned_start_date.data = p.planned_start_date or date.today()
        form.planned_end_date.data = p.planned_end_date or date.today()
        form.expected_income_yuan.data = cents_to_yuan(int(p.expected_income_cents))
        form.referral_ratio_percent.data = (
            (Decimal(p.referral_ratio or 0) * Decimal("100")).quantize(Decimal("0.0001"))
        )
        form.broker_fee_mode.data = getattr(p, "broker_fee_mode", None) or BROKER_MODE_PERCENT
        form.broker_fee_direction.data = (
            getattr(p, "broker_fee_direction", None) or BROKER_DIR_WE_PAY
        )
        if (getattr(p, "broker_fee_mode", None) or BROKER_MODE_PERCENT) == BROKER_MODE_PERCENT:
            form.broker_fixed_fee_yuan.data = Decimal("0")
        else:
            form.broker_fixed_fee_yuan.data = cents_to_yuan(
                int(getattr(p, "broker_fixed_fee_cents", 0) or 0)
            )
        form.status.data = p.status
        form.note.data = p.note or ""
        form.parent_project_id.data = p.parent_project_id or 0
        form.can_dividend.data = 1 if p.can_dividend else 0
        current_members = ProjectMember.query.filter_by(project_id=p.id).all()
        form.member_user_ids.data = [m.user_id for m in current_members]

    if form.validate_on_submit():
        ratio_percent = form.referral_ratio_percent.data
        ratio = Decimal("0")
        if ratio_percent is not None:
            ratio = (Decimal(str(ratio_percent)) / Decimal("100")).quantize(
                Decimal("0.000001")
            )
        mode = form.broker_fee_mode.data
        direction = form.broker_fee_direction.data
        fixed_yuan = form.broker_fixed_fee_yuan.data or Decimal("0")
        fixed_cents = yuan_to_cents(fixed_yuan) if fixed_yuan > 0 else 0
        if mode == BROKER_MODE_PERCENT:
            fixed_cents = 0

        # 校验项目名称唯一性（排除自身）
        name = form.name.data.strip()
        existing = Project.query.filter(Project.name == name, Project.id != p.id).first()
        if existing:
            flash(f"项目名称「{name}」已存在，请换一个名称", "warning")
            return render_template("project_form.html", form=form, is_admin=is_admin(), mode="edit", project=p,
                                   parent_candidates=parent_candidates, admin_user_ids=admin_user_ids, parent_member_ids=parent_member_ids)
        if (
            mode == BROKER_MODE_PERCENT
            and direction == BROKER_DIR_NET_FROM_BROKER
            and ratio >= Decimal("1")
        ):
            flash("「中介从客户款中扣除」且按比例时，中介比例须小于 100%。", "warning")
            return render_template(
                "project_form.html",
                form=form,
                is_admin=is_admin(),
                mode="edit",
                project=p,
            )

        p.name = name
        p.leader_user_id = int(form.leader_user_id.data)
        p.expected_income_cents = yuan_to_cents(form.expected_income_yuan.data)
        p.referral_ratio = ratio
        p.broker_fee_mode = mode
        p.broker_fee_direction = direction
        p.broker_fixed_fee_cents = int(fixed_cents)
        _apply_project_status(p, form.status.data)
        p.note = form.note.data or None
        parent_id = form.parent_project_id.data
        p.parent_project_id = parent_id if parent_id and parent_id != 0 else None
        p.can_dividend = form.can_dividend.data == 1

        new_member_ids = set(form.member_user_ids.data or [])
        new_member_ids.update(admin_user_ids)  # admin 不可移出项目
        new_member_ids.add(current_user.id)
        new_member_ids.add(int(p.leader_user_id))
        existing = ProjectMember.query.filter_by(project_id=p.id).all()
        existing_ids = {m.user_id for m in existing}
        for m in existing:
            if m.user_id not in new_member_ids:
                db.session.delete(m)
        for uid in new_member_ids:
            if uid not in existing_ids:
                db.session.add(ProjectMember(project_id=p.id, user_id=int(uid)))

        _log_project_activity(
            int(p.id),
            "project.update",
            f"编辑项目资料（名称、成员、合同金额、状态等）",
        )
        db.session.commit()
        flash("项目已更新", "success")
        return redirect(url_for("main.project_detail", project_id=p.id))

    parent_member_ids = _get_parent_member_ids()

    return render_template(
        "project_form.html",
        form=form,
        is_admin=is_admin(),
        mode="edit",
        project=p,
        parent_candidates=parent_candidates,
        admin_user_ids=admin_user_ids,
        parent_member_ids=parent_member_ids,
        parent_members_map_json=json.dumps(parent_members_map, ensure_ascii=False),
    )


@bp.post("/projects/<int:project_id>/delete")
@login_required
def projects_delete(project_id: int):
    flash("删除流程已升级：请在项目页发起删除申请并收集全员同意后执行删除。", "info")
    return redirect(url_for("main.project_detail", project_id=project_id))


@bp.route("/projects/<int:project_id>/progress", methods=["GET"])
@login_required
def project_progress(project_id: int):
    return redirect(url_for("main.project_logs", project_id=project_id))


@bp.route("/projects/<int:project_id>/logs", methods=["GET"])
@login_required
def project_logs(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))
    if not is_admin() and not _can_view_project(int(current_user.id), project_id):
        flash("无权查看", "danger")
        return redirect(url_for("main.projects_list"))

    log_id = request.args.get("log_id", type=int)
    all_logs = (
        ProjectUpdate.query.options(joinedload(ProjectUpdate.author))
        .filter_by(project_id=p.id)
        .order_by(ProjectUpdate.created_at.desc())
        .all()
    )
    current_log = None
    if log_id:
        current_log = next((l for l in all_logs if l.id == log_id), None)
    elif all_logs:
        current_log = all_logs[0]
    if current_log:
        # Eager load attachments for the selected log
        current_log = ProjectUpdate.query.options(
            joinedload(ProjectUpdate.author), joinedload(ProjectUpdate.attachments)
        ).filter_by(id=current_log.id).first()

    return render_template("project_logs.html",
                          project=p, all_logs=all_logs, current_log=current_log, is_admin=is_admin())


@bp.route("/projects/<int:project_id>/logs/new", methods=["GET", "POST"])
@login_required
def project_logs_new(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))
    if not _is_project_member(p.id, current_user.id):
        flash("你不是该项目成员", "danger")
        return redirect(url_for("main.projects_list"))

    form = ProjectUpdateForm()
    if form.validate_on_submit():
        u = ProjectUpdate(
            project_id=p.id,
            title=form.title.data or None,
            body=form.body.data,
            created_by_user_id=current_user.id,
        )
        db.session.add(u)
        db.session.flush()
        n_att = _save_project_update_attachments(u)
        db.session.commit()
        return redirect(url_for("main.project_logs", project_id=p.id, log_id=u.id))

    return render_template("project_logs_new.html", project=p, form=form, is_admin=is_admin())


@bp.post("/projects/<int:project_id>/log-rules/add")
@login_required
def project_log_rule_add(project_id: int):
    """新增/更新一条日志撰写规则（同一个人重复 add 视为改频率）。"""
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))
    if not _can_manage_log_rules(p):
        flash("无权限设置日志撰写规则", "danger")
        return redirect(url_for("main.project_detail", project_id=p.id))

    user_id = request.form.get("user_id", type=int)
    day_set = {
        int(d)
        for d in request.form.getlist("weekdays")
        if str(d).strip().isdigit() and 1 <= int(d) <= 7
    }
    member_ids = _project_member_user_ids(p.id)
    if not user_id or int(user_id) not in member_ids:
        flash("请选择该项目的成员", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))
    if not day_set:
        flash("请至少选择一个星期（周一~周日）", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    u = db.session.get(User, int(user_id))
    rule = ProjectLogRule.query.filter_by(project_id=p.id, user_id=int(user_id)).first()
    is_new = rule is None
    if rule is None:
        rule = ProjectLogRule(
            project_id=p.id,
            user_id=int(user_id),
            created_by_user_id=current_user.id,
        )
        db.session.add(rule)
    rule.weekdays = weekdays_to_csv(day_set)
    rule.is_active = True

    label = weekdays_label(rule.weekdays)
    _log_project_activity(
        int(p.id),
        "project.log_rule",
        f"{'新增' if is_new else '修改'}日志撰写规则：{(u.username if u else user_id)} → {label}",
    )
    db.session.commit()
    flash(f"已{'添加' if is_new else '更新'}日志撰写规则：{(u.username if u else user_id)} {label}", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.post("/projects/<int:project_id>/log-rules/<int:rule_id>/delete")
@login_required
def project_log_rule_delete(project_id: int, rule_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))
    if not _can_manage_log_rules(p):
        flash("无权限设置日志撰写规则", "danger")
        return redirect(url_for("main.project_detail", project_id=p.id))

    rule = db.session.get(ProjectLogRule, rule_id)
    if not rule or int(rule.project_id) != int(p.id):
        flash("规则不存在", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    username = rule.user.username if rule.user else rule.user_id
    db.session.delete(rule)
    _log_project_activity(
        int(p.id), "project.log_rule", f"删除日志撰写规则：{username}"
    )
    db.session.commit()
    flash(f"已删除 {username} 的日志撰写规则", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.route("/projects/<int:project_id>/updates", methods=["POST"])
@login_required
def project_update_post(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if not _check_project_not_ended(p):
        return redirect(url_for("main.project_detail", project_id=p.id))
    if not _is_project_member(p.id, current_user.id):
        flash("你不是该项目成员，无法提交进展", "danger")
        return redirect(url_for("main.projects_list"))

    form = ProjectUpdateForm()
    if not form.validate_on_submit():
        flash("进展内容不合法", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    u = ProjectUpdate(
        project_id=p.id,
        title=form.title.data or None,
        body=form.body.data,
        created_by_user_id=current_user.id,
    )
    db.session.add(u)
    db.session.flush()
    n_att = _save_project_update_attachments(u)
    db.session.commit()
    flash("进展已提交", "success")
    return redirect(url_for("main.project_progress", project_id=p.id))


@bp.get("/projects/<int:project_id>")
@login_required
def project_detail(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if not is_admin() and not _can_view_project(int(current_user.id), project_id):
        flash("你不是该项目成员，无权查看。", "danger")
        return redirect(url_for("main.projects_list"))

    # 父项目：聚合所有子项目数据
    is_parent = bool(p.sub_projects)
    if is_parent:
        all_ids = _collect_all_sub_project_ids(int(p.id))
        query_pid_filter = Transaction.project_id.in_(all_ids)
    else:
        query_pid_filter = Transaction.project_id == p.id

    tx_page = request.args.get("tx_page", 1, type=int) or 1
    tx_page = max(tx_page, 1)
    tx_pagination = (
        Transaction.query.options(joinedload(Transaction.attachments), joinedload(Transaction.created_by), joinedload(Transaction.recipient_user))
        .filter(query_pid_filter)
        .filter(Transaction.is_void.is_(False), Transaction.status == "active")
        .order_by(Transaction.occur_date.desc(), Transaction.id.desc())
        .paginate(page=tx_page, per_page=40, error_out=False)
    )
    txs = tx_pagination.items

    pending_txs = (
        Transaction.query.options(joinedload(Transaction.attachments), joinedload(Transaction.created_by), joinedload(Transaction.recipient_user))
        .filter(query_pid_filter)
        .filter(Transaction.is_void.is_(False), Transaction.status == "pending")
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .limit(50)
        .all()
    )
    open_edit_reqs = (
        TransactionEditRequest.query.filter_by(status="open")
        .join(Transaction, Transaction.id == TransactionEditRequest.transaction_id)
        .filter(query_pid_filter)
        .order_by(TransactionEditRequest.id.desc())
        .all()
    )
    edit_req_by_tx = {r.transaction_id: r for r in open_edit_reqs}
    edit_ready_by_tx = {tid: _transaction_edit_fully_approved(r) for tid, r in edit_req_by_tx.items()}
    edit_can_execute_by_tx = {
        tid: _transaction_edit_can_execute(r) for tid, r in edit_req_by_tx.items()
    }

    open_delete_reqs = (
        TransactionDeleteRequest.query.filter_by(status="open")
        .join(Transaction, Transaction.id == TransactionDeleteRequest.transaction_id)
        .filter(query_pid_filter)
        .order_by(TransactionDeleteRequest.id.desc())
        .all()
    )
    delete_req_by_tx = {r.transaction_id: r for r in open_delete_reqs}
    delete_ready_by_tx = {
        tid: _transaction_delete_fully_approved(r) for tid, r in delete_req_by_tx.items()
    }
    delete_can_execute_by_tx = {
        tid: _transaction_delete_can_execute(r) for tid, r in delete_req_by_tx.items()
    }

    pending_create_req_by_tx: dict[int, TransactionCreateRequest] = {}
    create_ready_by_tx: dict[int, bool] = {}
    create_can_execute_by_tx: dict[int, bool] = {}
    if pending_txs:
        pending_ids = [int(t.id) for t in pending_txs]
        open_creqs = (
            TransactionCreateRequest.query.filter(
                TransactionCreateRequest.transaction_id.in_(pending_ids),
                TransactionCreateRequest.status == "open",
            )
            .order_by(TransactionCreateRequest.id.desc())
            .all()
        )
        pending_create_req_by_tx = {int(r.transaction_id): r for r in open_creqs}
        create_ready_by_tx = {
            tid: _transaction_create_fully_approved(r)
            for tid, r in pending_create_req_by_tx.items()
        }
        create_can_execute_by_tx = {
            tid: _transaction_create_can_execute(r)
            for tid, r in pending_create_req_by_tx.items()
        }
    adjustments = (
        ProjectExpectedIncomeAdjustment.query.options(joinedload(ProjectExpectedIncomeAdjustment.approvals))
        .filter(ProjectExpectedIncomeAdjustment.project_id.in_(all_ids) if is_parent else ProjectExpectedIncomeAdjustment.project_id == p.id)
        .order_by(ProjectExpectedIncomeAdjustment.created_at.desc())
        .all()
    )
    members = (
        db.session.query(User)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .filter(ProjectMember.project_id == p.id)
        .order_by(User.username.asc())
        .all()
    )
    # 项目人员显示：过滤掉非负责人的管理员（admin不参与实际工作）
    leader_id = int(p.leader_user_id or 0)
    display_members = [u for u in members if u.role != Role.ADMIN.value or u.id == leader_id]
    delete_req = (
        ProjectDeleteRequest.query.filter_by(project_id=p.id, status="open")
        .order_by(ProjectDeleteRequest.id.desc())
        .first()
    )
    approved_user_ids = set()
    if delete_req:
        approved_user_ids = {a.user_id for a in delete_req.approvals}
    non_admin_member_ids = {u.id for u in members if u.role != Role.ADMIN.value}
    delete_ready = bool(delete_req) and bool(non_admin_member_ids) and non_admin_member_ids.issubset(approved_user_ids)

    received_cents = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(
            query_pid_filter,
            Transaction.type == TransactionType.INCOME.value,
            Transaction.settled.is_(True),
            Transaction.is_void.is_(False),
            Transaction.status == "active",
        )
        .scalar()
        or 0
    )
    paid_cents_all = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(
            query_pid_filter,
            Transaction.type == TransactionType.EXPENSE.value,
            Transaction.settled.is_(True),
            Transaction.is_void.is_(False),
            Transaction.status == "active",
        )
        .scalar()
        or 0
    )
    dividend_expense_cents = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(
            query_pid_filter,
            Transaction.type == TransactionType.EXPENSE.value,
            Transaction.settled.is_(True),
            Transaction.is_void.is_(False),
            Transaction.status == "active",
            db.func.coalesce(Transaction.note, "").like("[DIVIDEND]%"),
        )
        .scalar()
        or 0
    )
    paid_cents = int(paid_cents_all)
    # 实际已支付的中介费（支出流水，对方=中介费）：算「未收净额」要用它
    broker_paid_cents = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(
            query_pid_filter,
            Transaction.type == TransactionType.EXPENSE.value,
            Transaction.counterparty == CP_EXPENSE_BROKER,
            Transaction.settled.is_(True),
            Transaction.is_void.is_(False),
            Transaction.status == "active",
        )
        .scalar()
        or 0
    )
    if is_parent:
        expected_total_cents = sum(project_expected_total_cents(sid) for sid in all_ids)
        finance = build_project_finance(p, expected_total_cents=expected_total_cents,
                                        received_settled_income_bank_cents=int(received_cents),
                                        broker_paid_cents=broker_paid_cents)
        balance_tmp = {}
        for sid in all_ids:
            balance_tmp.update(running_settled_cash_balance_by_transaction_id(sid))
        balance_after_by_tx_id = balance_tmp
    else:
        expected_total_cents = int(project_expected_total_cents(p.id))
        finance = build_project_finance(
            p,
            expected_total_cents=expected_total_cents,
            received_settled_income_bank_cents=int(received_cents),
            broker_paid_cents=broker_paid_cents,
        )
        balance_after_by_tx_id = running_settled_cash_balance_by_transaction_id(p.id)
    expected_net_cents = finance["expected_net_cents"]
    received_net_cents = finance["received_net_cents"]
    remaining_gross_cents = finance["remaining_gross_cents"]
    remaining_net_cents = finance["remaining_net_cents"]
    remaining_net_cash_cents = finance["remaining_net_cash_cents"]
    broker_unpaid_cents = finance["broker_unpaid_cents"]
    broker_fee_expected_total_cents = finance["broker_fee_expected_total_cents"]
    broker_estimated_cents = finance["broker_estimated_on_received_cents"]

    profit_realized_cents = int(received_net_cents) - int(paid_cents)
    profit_if_fully_collected_cents = int(expected_net_cents) - int(paid_cents)
    # 分红口径统一走 dividends.dividend_metrics（唯一实现）
    dm = dividend_metrics(p.id, received_net_cents=int(received_net_cents))
    dividend_base_cents = dm.base_cents
    dividend_paid_total_cents = dm.paid_cents
    dividend_remaining_cents = dm.remaining_cents
    dividend_rows = (
        ProjectDividendDistribution.query.filter_by(project_id=p.id)
        .order_by(ProjectDividendDistribution.created_at.desc())
        .limit(20)
        .all()
    )

    # Dividend recipients dropdown (project members first, exclude admin)
    admin_ids = _admin_user_ids()
    member_options = [(f"u:{u.id}", f"{u.username}") for u in members if u.id not in admin_ids]
    saved_names = (
        ProjectDividendRecipient.query.filter_by(project_id=p.id)
        .order_by(ProjectDividendRecipient.name.asc())
        .all()
    )
    name_options = [(f"n:{r.name}", r.name) for r in saved_names]

    leader = db.session.get(User, int(p.leader_user_id)) if p.leader_user_id else None

    today = date.today()
    start_d = p.planned_start_date or today
    end_d = p.planned_end_date or start_d
    days_total = max((end_d - start_d).days, 0) + 1
    if today <= start_d:
        days_elapsed = 0
    elif today >= end_d:
        days_elapsed = days_total
    else:
        days_elapsed = (today - start_d).days + 1
    progress_pct = int((Decimal(days_elapsed) / Decimal(days_total) * Decimal("100")).quantize(Decimal("1"))) if days_total > 0 else 0
    progress_pct = max(0, min(100, progress_pct))
    days_left = (end_d - today).days
    overdue_days = max(0, (today - end_d).days)

    end_change_req = _open_project_end_date_change_request(p.id)
    end_change_approved_ids = set()
    if end_change_req:
        end_change_approved_ids = {a.user_id for a in end_change_req.approvals}
    end_change_ready = (
        _project_end_date_change_fully_approved(end_change_req) if end_change_req else False
    )
    end_change_can_execute = _project_end_date_change_can_execute(end_change_req)
    delete_can_execute = bool(delete_req) and (
        delete_ready or _approval_user_ids_include_any_admin(approved_user_ids)
    )

    end_change_form = ProjectEndDateChangeForm()
    if end_change_req and request.method == "GET":
        end_change_form.new_end_date.data = end_change_req.new_end_date

    # 终止项目申请
    end_req = _open_project_end_request(p.id)
    end_approved_ids = set()
    if end_req:
        end_approved_ids = {a.user_id for a in end_req.approvals}
    end_ready = _project_end_fully_approved(end_req) if end_req else False
    end_can_execute = _project_end_can_execute(end_req)

    end_revive_req = _open_project_revive_request(p.id)

    # 复活项目申请
    end_revive_req = (
        ProjectReviveRequest.query.filter_by(project_id=p.id, status="open")
        .order_by(ProjectReviveRequest.id.desc())
        .first()
    )

    project_updates = (
        ProjectUpdate.query.options(joinedload(ProjectUpdate.attachments), joinedload(ProjectUpdate.author))
        .filter_by(project_id=p.id)
        .order_by(ProjectUpdate.created_at.desc())
        .limit(50)
        .all()
    )
    update_form = ProjectUpdateForm()
    dividend_form = ProjectDividendForm()
    dividend_form.recipient_key.choices = [("", "（请选择）")] + member_options + name_options

    activity_logs = (
        ProjectActivityLog.query.options(joinedload(ProjectActivityLog.actor))
        .filter(ProjectActivityLog.project_id == p.id)
        .order_by(ProjectActivityLog.created_at.desc())
        .limit(200)
        .all()
    )

    # 构建用户ID→名称映射（用于显示项目人员姓名）
    user_name_map = {u.id: u.username for u in User.query.all()}

    # 递归收集所有子孙项目（用于父项目页展示）
    all_sub_projects = []
    if is_parent:
        sub_ids = _collect_all_sub_project_ids(int(p.id))
        sub_ids.discard(int(p.id))
        if sub_ids:
            all_sub_projects = Project.query.filter(Project.id.in_(sub_ids)).order_by(Project.name.asc()).all()

    return render_template(
        "project_detail.html",
        project=p,
        leader=leader,
        today=today,
        all_sub_projects=all_sub_projects,
        is_parent=is_parent,
        days_total=int(days_total),
        days_elapsed=int(days_elapsed),
        days_left=int(days_left),
        overdue_days=int(overdue_days),
        progress_pct=int(progress_pct),
        end_change_req=end_change_req,
        end_change_approved_ids=end_change_approved_ids,
        end_change_ready=end_change_ready,
        end_change_can_execute=end_change_can_execute,
        end_change_form=end_change_form,
        end_req=end_req,
        end_approved_ids=end_approved_ids,
        end_ready=end_ready,
        end_can_execute=end_can_execute,
        end_revive_req=end_revive_req,
        project_updates=project_updates,
        update_form=update_form,
        log_rule_rows=[
            {"rule": r, "label": weekdays_label(r.weekdays)}
            for r in rules_for_project(int(p.id))
        ],
        log_rule_members=display_members,
        weekday_options=[(d, WEEKDAY_LABELS[d]) for d in range(1, 8)],
        can_manage_log_rules=_can_manage_log_rules(p),
        transactions=txs,
        pending_transactions=pending_txs,
        create_req_by_tx=pending_create_req_by_tx,
        create_ready_by_tx=create_ready_by_tx,
        create_can_execute_by_tx=create_can_execute_by_tx,
        edit_req_by_tx=edit_req_by_tx,
        edit_ready_by_tx=edit_ready_by_tx,
        edit_can_execute_by_tx=edit_can_execute_by_tx,
        delete_req_by_tx=delete_req_by_tx,
        delete_ready_by_tx=delete_ready_by_tx,
        delete_can_execute_by_tx=delete_can_execute_by_tx,
        user_name_map=user_name_map,
        adjustments=adjustments,
        members=members,
        display_members=display_members,
        delete_req=delete_req,
        approved_user_ids=approved_user_ids,
        delete_ready=delete_ready,
        delete_can_execute=delete_can_execute,
        received_cents=int(received_cents),
        paid_cents=int(paid_cents),
        expected_total_cents=int(expected_total_cents),
        expected_net_cents=int(expected_net_cents),
        received_net_cents=int(received_net_cents),
        remaining_gross_cents=int(remaining_gross_cents),
        remaining_net_cents=int(remaining_net_cents),
        remaining_net_cash_cents=int(remaining_net_cash_cents),
        broker_unpaid_cents=int(broker_unpaid_cents),
        broker_fee_expected_total_cents=int(broker_fee_expected_total_cents),
        broker_estimated_cents=int(broker_estimated_cents),
        broker_paid_cents=int(broker_paid_cents),
        finance=finance,
        balance_after_by_tx_id=balance_after_by_tx_id,
        profit_realized_cents=int(profit_realized_cents),
        profit_if_fully_collected_cents=int(profit_if_fully_collected_cents),
        dividend_base_cents=int(dividend_base_cents),
        dividend_paid_total_cents=int(dividend_paid_total_cents),
        dividend_remaining_cents=int(dividend_remaining_cents),
        dividend_rows=dividend_rows,
        dividend_form=dividend_form,
        dividend_expense_cents=int(dividend_expense_cents),
        tx_pagination=tx_pagination,
        activity_logs=activity_logs,
        is_admin=is_admin(),
    )

