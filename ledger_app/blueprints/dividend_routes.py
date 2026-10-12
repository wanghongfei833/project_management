"""dividend_routes.py：路由实现（原 routes.py 拆分而来，URL/行为不变）。"""
from ._common import *  # noqa: F401,F403



@bp.route("/projects/<int:project_id>/dividend", methods=["GET"])
@login_required
def project_dividend_page(project_id: int):
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.project_detail", project_id=project_id))

    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if p.status != "ended":
        flash("只有已终止的项目才能进行分红", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    # 计算分红数据
    received_cents = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(Transaction.project_id == p.id, Transaction.type == TransactionType.INCOME.value,
                Transaction.settled.is_(True), Transaction.is_void.is_(False), Transaction.status == "active")
        .scalar() or 0
    )
    exp_total = project_expected_total_cents(p.id)
    fin = build_project_finance(p, expected_total_cents=exp_total, received_settled_income_bank_cents=received_cents)
    received_net = fin["received_net_cents"]
    # 分红口径统一走 dividends.dividend_metrics（唯一实现）
    dm = dividend_metrics(p.id, received_net_cents=int(received_net))

    # 分红对象：排除 admin 的项目成员
    members = (
        db.session.query(User)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .filter(ProjectMember.project_id == p.id, User.role != Role.ADMIN.value)
        .order_by(User.username.asc())
        .all()
    )

    form = ProjectDividendForm()
    member_options = [(f"u:{u.id}", f"{u.username}") for u in members]
    form.recipient_key.choices = [("", "（请选择成员）")] + member_options

    form_data = {
        "project": p,
        "received_net_cents": received_net,
        "paid_cents_excl_div": dm.paid_excl_dividend_cents,
        "dividend_base_cents": dm.base_cents,
        "dividend_paid_cents": dm.paid_cents,
        "dividend_remaining_cents": dm.remaining_cents,
        "members": members,
        "form": form,
    }
    return render_template("project_dividend.html", **form_data, is_admin=is_admin())


@bp.post("/projects/<int:project_id>/dividend")
@login_required
def project_dividend_post(project_id: int):
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.project_detail", project_id=project_id))

    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))

    if p.status != "ended":
        flash("只有已终止的项目才能进行分红", "warning")
        return redirect(url_for("main.project_detail", project_id=p.id))

    # 获取非 admin 成员
    members = (
        db.session.query(User)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .filter(ProjectMember.project_id == p.id, User.role != Role.ADMIN.value)
        .order_by(User.username.asc())
        .all()
    )

    # 读取前端提交的金额（amount_<user_id>）
    dividend_items = []
    total_amt = 0
    for u in members:
        key = f"amount_{u.id}"
        raw = request.form.get(key, "").strip()
        if raw:
            try:
                amt_cents = yuan_to_cents(Decimal(raw))
            except Exception:
                continue
            if amt_cents > 0:
                dividend_items.append((u, amt_cents))
                total_amt += amt_cents

    if not dividend_items:
        flash("请至少填写一个成员的分红金额", "warning")
        return redirect(url_for("main.project_dividend_page", project_id=p.id))

    # 校验剩余可分红
    received_cents = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(Transaction.project_id == p.id, Transaction.type == TransactionType.INCOME.value,
                Transaction.settled.is_(True), Transaction.is_void.is_(False), Transaction.status == "active")
        .scalar() or 0
    )
    fin = build_project_finance(p, expected_total_cents=project_expected_total_cents(p.id),
                                received_settled_income_bank_cents=received_cents)
    dm = dividend_metrics(p.id, received_net_cents=int(fin["received_net_cents"]))
    div_base = dm.base_cents
    div_remaining = dm.remaining_cents

    if total_amt > div_remaining:
        flash(f"分红总额超出剩余可分红额度（剩余 ¥{format_cents(div_remaining)}）", "danger")
        return redirect(url_for("main.project_dividend_page", project_id=p.id))

    # 二次校验：在事务中重新确认剩余可分红（防并发超分）
    dm_latest = dividend_metrics(p.id, received_net_cents=int(fin["received_net_cents"]))
    latest_div_remaining = dm_latest.remaining_cents
    if total_amt > latest_div_remaining:
        flash(f"分红额度已发生变化（剩余 ¥{format_cents(latest_div_remaining)}），请重新提交", "danger")
        return redirect(url_for("main.project_dividend_page", project_id=p.id))

    # 校验现金结余（只算已生效流水，避免待审流水把额度算错）
    cash_balance = _dividend_cash_balance_cents(int(p.id))
    if total_amt > cash_balance:
        flash(f"分红金额超出项目当前可用结余（结余 ¥{format_cents(cash_balance)}）", "danger")
        return redirect(url_for("main.project_dividend_page", project_id=p.id))

    # 创建一笔聚合支出流水
    tx = Transaction(
        project_id=p.id,
        type=TransactionType.EXPENSE.value,
        amount_cents=int(total_amt),
        occur_date=date.today(),
        settled=True,
        counterparty="批量分红",
        note=f"[DIVIDEND] 批量分红给 {len(dividend_items)} 人",
        created_by_user_id=current_user.id,
    )
    db.session.add(tx)
    db.session.flush()

    for u, amt in dividend_items:
        db.session.add(ProjectDividendDistribution(
            transaction_id=tx.id, project_id=p.id,
            recipient_user_id=u.id, recipient_name=u.username,
            amount_cents=int(amt), created_by_user_id=current_user.id,
        ))

    member_names = "、".join(u.username for u, _ in dividend_items)
    _log_project_activity(
        int(p.id), "dividend.payout",
        f"批量分红 ¥{format_cents(int(total_amt))} → {member_names}",
        detail=f"transaction_id={tx.id}",
    )
    db.session.commit()
    flash(f"分红完成：¥{format_cents(int(total_amt))} 分给 {len(dividend_items)} 人", "success")
    return redirect(url_for("main.project_dividend_page", project_id=p.id))

