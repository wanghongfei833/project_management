"""exports.py：路由实现（原 routes.py 拆分而来，URL/行为不变）。"""
from ._common import *  # noqa: F401,F403



@bp.get("/search")
@login_required
def search():
    """全局搜索：流水（备注/对方/收款人/发起人/金额/编号）、凭证文件名、项目名。"""
    q_text = (request.args.get("q") or "").strip()
    txs: list = []
    att_txs: list = []
    projects: list = []
    if q_text:
        like = f"%{q_text}%"
        try:
            amount_cents = yuan_to_cents(
                Decimal(q_text.replace(",", "").replace("¥", "").replace("元", "").strip())
            )
        except Exception:
            amount_cents = None

        conds = [
            db.func.coalesce(Transaction.note, "").like(like),
            db.func.coalesce(Transaction.counterparty, "").like(like),
            Transaction.id == (int(q_text) if q_text.isdigit() else -1),
        ]
        if amount_cents and amount_cents > 0:
            conds.append(Transaction.amount_cents == amount_cents)
        member_ids = db.session.query(User.id).filter(User.username.like(like))
        conds.append(Transaction.recipient_user_id.in_(member_ids))
        conds.append(Transaction.created_by_user_id.in_(member_ids))

        base = Transaction.query.options(
            joinedload(Transaction.project), joinedload(Transaction.recipient_user)
        ).filter(Transaction.project_id.isnot(None), db.or_(*conds))
        base = _apply_transaction_project_access_filter(base)
        txs = base.order_by(Transaction.occur_date.desc(), Transaction.id.desc()).limit(100).all()

        att_ids = [
            int(r[0])
            for r in db.session.query(Attachment.transaction_id)
            .filter(Attachment.filename.like(like))
            .limit(200)
            .all()
        ]
        if att_ids:
            aq = Transaction.query.options(
                joinedload(Transaction.project), joinedload(Transaction.attachments)
            ).filter(Transaction.id.in_(att_ids))
            aq = _apply_transaction_project_access_filter(aq)
            att_txs = aq.order_by(Transaction.occur_date.desc(), Transaction.id.desc()).limit(50).all()

        projects = (
            _accessible_projects_query()
            .filter(Project.name.like(like))
            .order_by(Project.id.desc())
            .limit(50)
            .all()
        )

    return render_template(
        "search.html",
        q=q_text,
        transactions=txs,
        attachment_transactions=att_txs,
        projects=projects,
        total=len(txs) + len(att_txs) + len(projects),
        is_admin=is_admin(),
    )


@bp.get("/export/transactions.csv")
@login_required
def export_transactions_csv():
    """导出流水（含作废/待审，用「状态」列区分），权限与列表页一致。"""
    q = Transaction.query.options(
        joinedload(Transaction.project), joinedload(Transaction.created_by)
    ).filter(Transaction.project_id.isnot(None))
    q = _apply_transaction_project_access_filter(q)

    project_id = request.args.get("project_id", type=int)
    if project_id:
        if not is_admin() and not _is_project_member(project_id, current_user.id):
            flash("你不是该项目成员，无权导出该项目", "danger")
            return redirect(url_for("main.transactions_list"))
        q = q.filter(Transaction.project_id == project_id)
    start_d, end_d = _export_date_range()
    if start_d:
        q = q.filter(Transaction.occur_date >= start_d)
    if end_d:
        q = q.filter(Transaction.occur_date <= end_d)

    txs = q.order_by(Transaction.occur_date.asc(), Transaction.id.asc()).all()
    rows = []
    for t in txs:
        if t.is_void:
            status = "已作废"
        elif t.status == "pending":
            status = "待审核"
        else:
            status = "已生效"
        rows.append(
            [
                str(t.occur_date),
                (t.project.name if t.project else ""),
                "收入" if t.type == TransactionType.INCOME.value else "支出",
                "%.2f" % (int(t.amount_cents or 0) / 100),
                "已到账" if t.settled else "未到账",
                status,
                t.counterparty or "",
                (t.recipient_user.username if t.recipient_user else ""),
                (t.created_by.username if t.created_by else ""),
                t.note or "",
                str(len(t.attachments)) if t.attachments else "0",
            ]
        )
    header = [
        "日期", "项目", "类型", "金额(元)", "到账", "状态",
        "对方", "项目人员", "发起人", "备注", "凭证数",
    ]
    return _csv_response(header, rows, f"transactions-{date.today():%Y%m%d}.csv")


@bp.get("/export/projects.csv")
@login_required
def export_projects_csv():
    """导出项目财务汇总（口径与项目详情页/仪表盘一致）。"""
    projects = _accessible_projects_query().order_by(Project.id.asc()).all()
    broker_paid = {
        int(pid): int(total or 0)
        for pid, total in (
            db.session.query(
                Transaction.project_id,
                db.func.coalesce(db.func.sum(Transaction.amount_cents), 0),
            )
            .filter(
                Transaction.type == TransactionType.EXPENSE.value,
                Transaction.counterparty == CP_EXPENSE_BROKER,
                Transaction.settled.is_(True),
                Transaction.is_void.is_(False),
                Transaction.status == "active",
                Transaction.project_id.isnot(None),
            )
            .group_by(Transaction.project_id)
            .all()
        )
    }

    rows = []
    for p in projects:

        def _sum(*filters) -> int:
            return int(
                db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
                .filter(*filters)
                .scalar()
                or 0
            )

        base = [Transaction.project_id == p.id, Transaction.is_void.is_(False), Transaction.status == "active"]
        received = _sum(*base, Transaction.type == TransactionType.INCOME.value, Transaction.settled.is_(True))
        expense = _sum(*base, Transaction.type == TransactionType.EXPENSE.value, Transaction.settled.is_(True))
        expected_total = int(project_expected_total_cents(int(p.id)))
        fin = build_project_finance(
            p,
            expected_total_cents=expected_total,
            received_settled_income_bank_cents=received,
            broker_paid_cents=broker_paid.get(int(p.id), 0),
        )
        dm = dividend_metrics(int(p.id), received_net_cents=fin["received_net_cents"])
        rows.append(
            [
                p.name,
                "已终止" if p.status == "ended" else "进行中",
                "%.2f" % (expected_total / 100),
                "%.2f" % (fin["broker_fee_expected_total_cents"] / 100),
                "%.2f" % (fin["expected_net_cents"] / 100),
                "%.2f" % (fin["received_bank_income_cents"] / 100),
                "%.2f" % (broker_paid.get(int(p.id), 0) / 100),
                "%.2f" % (fin["broker_unpaid_cents"] / 100),
                "%.2f" % (fin["remaining_gross_cents"] / 100),
                "%.2f" % (fin["remaining_net_cash_cents"] / 100),
                "%.2f" % (expense / 100),
                "%.2f" % ((received - expense - fin["broker_unpaid_cents"]) / 100),
                "%.2f" % (dm.paid_cents / 100),
                "%.2f" % (dm.remaining_cents / 100),
            ]
        )
    header = [
        "项目", "状态", "应收(含介绍费)", "介绍费", "我方净应收", "已收",
        "已付中介费", "还欠中介费", "剩余应收", "未收净额(我方)",
        "支出", "净利润", "已分红", "剩余可分红",
    ]
    return _csv_response(header, rows, f"projects-{date.today():%Y%m%d}.csv")


@bp.get("/earnings")
@login_required
def personal_earnings():
    """个人收入查看：admin 看全部，普通用户只看自己。

    时间筛选按【项目实际终止时间】(projects.ended_at) 生效：首次进入默认本年；
    一旦限定区间，未终止（ended_at 为空）的项目不参与统计。
    """
    is_admin_user = is_admin()
    target_user_id = request.args.get("user_id", type=int)

    start_d, end_d = _earnings_range_from_request(request.args)
    # 用户填的是「北京时间」的日期，而 ended_at 存的是 naive UTC，所以边界要回推 8 小时；
    # 否则北京时间凌晨 0~8 点终止的项目会被算到前一天。
    lo = (
        datetime.combine(start_d, datetime.min.time()) - TZ_OFFSET
        if start_d
        else None
    )
    hi = (
        datetime.combine(end_d + timedelta(days=1), datetime.min.time()) - TZ_OFFSET
        if end_d
        else None
    )
    ranged = lo is not None or hi is not None

    if is_admin_user and target_user_id:
        users_q = [db.session.get(User, target_user_id)]
        users_q = [u for u in users_q if u]
    elif is_admin_user:
        users_q = User.query.filter_by(is_active=True).order_by(User.username.asc()).all()
    else:
        users_q = User.query.filter(User.id == current_user.id).all()
    rows = []
    for u in users_q:
        tx_payments = (
            db.session.query(Transaction.project_id,
                db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
            .filter(Transaction.recipient_user_id == u.id, Transaction.is_void.is_(False),
                    Transaction.status == "active", Transaction.settled.is_(True),
                    Transaction.type == "expense")
            .group_by(Transaction.project_id).all())
        div_payments = (
            db.session.query(ProjectDividendDistribution.project_id,
                db.func.coalesce(db.func.sum(ProjectDividendDistribution.amount_cents), 0))
            .filter(ProjectDividendDistribution.recipient_user_id == u.id)
            .group_by(ProjectDividendDistribution.project_id).all())
        project_totals = {}
        for pid, amt in tx_payments:
            project_totals[int(pid)] = {"payment": int(amt), "dividend": 0}
        for pid, amt in div_payments:
            p_int = int(pid)
            if p_int in project_totals:
                project_totals[p_int]["dividend"] = int(amt)
            else:
                project_totals[p_int] = {"payment": 0, "dividend": int(amt)}
        tp = td = 0
        pd = []
        for pid, vals in sorted(project_totals.items()):
            p = db.session.get(Project, pid)
            if not p:
                continue
            if ranged:
                if p.ended_at is None:
                    continue
                if lo is not None and p.ended_at < lo:
                    continue
                if hi is not None and p.ended_at >= hi:
                    continue
            pd.append({"project_name": p.name, "payment": vals["payment"], "dividend": vals["dividend"]})
            tp += vals["payment"]; td += vals["dividend"]
        rows.append({"user": u, "projects": pd, "total_payment": tp, "total_dividend": td, "grand_total": tp + td})
    return render_template(
        "personal_earnings.html",
        rows=rows,
        is_admin=is_admin_user,
        start=start_d.isoformat() if start_d else "",
        end=end_d.isoformat() if end_d else "",
        ranged=ranged,
        selected_user_id=target_user_id,
    )

