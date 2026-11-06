"""项目资金池：设置、公户/资金池划转及撤销。"""
from ._common import *  # noqa: F401,F403

from ..services.payments import can_pay_for_project


def _load_project(project_id: int):
    project = db.session.get(Project, int(project_id))
    if project is None:
        flash("项目不存在", "warning")
        return None
    if not can_pay_for_project(project.id):
        flash("只有项目负责人、管理员或财务可以管理资金池", "danger")
        return None
    return project


@bp.post("/projects/<int:project_id>/fund-pool/settings")
@login_required
def fund_pool_settings(project_id: int):
    project = _load_project(project_id)
    if project is None:
        return redirect(url_for("main.projects_list"))

    pool = ensure_project_fund_pool(project)
    members = (
        db.session.query(User)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .filter(
            ProjectMember.project_id == int(project.id),
            User.is_active.is_(True),
        )
        .order_by(User.username.asc())
        .all()
    )
    form = FundPoolSettingsForm()
    form.custodian_user_id.choices = [(int(u.id), u.username) for u in members]
    if request.method == "GET":
        form.name.data = pool.name
        form.custodian_user_id.data = int(
            pool.custodian_user_id or project.leader_user_id or 0
        )
    if not form.validate_on_submit():
        flash("资金池设置不合法，请检查保管人", "warning")
        return redirect(url_for("main.project_detail", project_id=project.id))

    pool.name = (form.name.data or "").strip()[:128] or "项目资金池"
    pool.custodian_user_id = int(form.custodian_user_id.data)
    _log_project_activity(
        int(project.id),
        "fund.settings",
        f"更新资金池设置：{pool.name}，保管人 {pool.custodian.username if pool.custodian else '—'}",
        detail=f"fund_pool_id={pool.id}",
    )
    db.session.commit()
    flash("资金池设置已保存", "success")
    return redirect(url_for("main.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/fund-pool/transfer")
@login_required
def fund_pool_transfer(project_id: int):
    project = _load_project(project_id)
    if project is None:
        return redirect(url_for("main.projects_list"))
    if not _check_project_not_ended(project):
        return redirect(url_for("main.project_detail", project_id=project.id))

    form = FundTransferForm()
    if not form.validate_on_submit():
        flash("资金划转信息不完整或金额不合法", "warning")
        return redirect(url_for("main.project_detail", project_id=project.id))

    pool = ensure_project_fund_pool(project)
    from_account, to_account = transfer_accounts(form.direction.data)
    amount_cents = yuan_to_cents(form.amount_yuan.data)
    balances = project_fund_balances([int(project.id)])
    available = account_balance(balances, from_account)
    if amount_cents > available:
        flash(
            f"{fund_account_label(from_account)}余额不足："
            f"当前 ¥{format_cents(available)}，本次需要 ¥{format_cents(amount_cents)}",
            "danger",
        )
        return redirect(url_for("main.project_detail", project_id=project.id))

    transfer = FundTransfer(
        project_id=int(project.id),
        pool_id=int(pool.id),
        from_account=from_account,
        to_account=to_account,
        amount_cents=int(amount_cents),
        occur_date=form.occur_date.data,
        method=(form.method.data or "银行转账")[:32],
        note=(form.note.data or "").strip() or None,
        created_by_user_id=int(current_user.id),
    )
    db.session.add(transfer)
    db.session.flush()
    added = _save_fund_transfer_attachments(transfer)
    _log_project_activity(
        int(project.id),
        "fund.transfer",
        f"{fund_account_label(from_account)} → {fund_account_label(to_account)} "
        f"¥{format_cents(amount_cents)}（{transfer.method}）"
        + (f"，凭证 {added} 个" if added else ""),
        detail=f"fund_transfer_id={transfer.id}",
    )
    db.session.commit()
    flash(
        f"已划转 ¥{format_cents(amount_cents)}："
        f"{fund_account_label(from_account)} → {fund_account_label(to_account)}",
        "success",
    )
    return redirect(url_for("main.project_detail", project_id=project.id))


@bp.post("/projects/<int:project_id>/fund-transfers/<int:transfer_id>/void")
@login_required
def fund_pool_transfer_void(project_id: int, transfer_id: int):
    project = _load_project(project_id)
    if project is None:
        return redirect(url_for("main.projects_list"))
    transfer = db.session.get(FundTransfer, int(transfer_id))
    if (
        transfer is None
        or int(transfer.project_id) != int(project.id)
        or transfer.is_void
    ):
        flash("资金划转记录不存在或已撤销", "warning")
        return redirect(url_for("main.project_detail", project_id=project.id))

    transfer.is_void = True
    _log_project_activity(
        int(project.id),
        "fund.transfer_void",
        f"撤销资金划转 #{transfer.id}："
        f"{fund_account_label(transfer.from_account)} → "
        f"{fund_account_label(transfer.to_account)} "
        f"¥{format_cents(int(transfer.amount_cents))}",
        detail=f"fund_transfer_id={transfer.id}",
    )
    db.session.commit()
    flash("资金划转已撤销，余额已自动恢复", "success")
    return redirect(url_for("main.project_detail", project_id=project.id))
