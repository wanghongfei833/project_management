"""dashboard.py：路由实现（原 routes.py 拆分而来，URL/行为不变）。"""
from ._common import *  # noqa: F401,F403



@bp.get("/")
@login_required
def dashboard():
    income_q = db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0)).filter(
        Transaction.type == TransactionType.INCOME.value,
        Transaction.settled.is_(True),
        Transaction.is_void.is_(False),
        Transaction.status == "active",
        Transaction.project_id.isnot(None),
    )
    income_q = _apply_transaction_project_access_filter(income_q)
    income_cents = income_q.scalar()
    expense_q = db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0)).filter(
        Transaction.type == TransactionType.EXPENSE.value,
        Transaction.settled.is_(True),
        Transaction.is_void.is_(False),
        Transaction.status == "active",
        Transaction.project_id.isnot(None),
    )
    expense_q = _apply_transaction_project_access_filter(expense_q)
    expense_cents = expense_q.scalar()

    projects = _accessible_projects_query().order_by(Project.id.desc()).all()
    # 各项目「实际已付中介费」（一次查完，避免逐个项目查库）
    broker_paid_by_project = {
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
    active_rows = []
    ended_rows = []
    active_count = 0
    ended_count = 0
    for p in projects:
        if p.status == "ended":
            ended_count += 1
        else:
            active_count += 1
        received_gross = (
            db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
            .filter(
                Transaction.project_id == p.id,
                Transaction.type == TransactionType.INCOME.value,
                Transaction.settled.is_(True),
                Transaction.is_void.is_(False),
                Transaction.status == "active",
            )
            .scalar()
        )
        received_gross = int(received_gross or 0)
        expected_total_cents = int(project_expected_total_cents(p.id))
        fin = build_project_finance(
            p,
            expected_total_cents=expected_total_cents,
            received_settled_income_bank_cents=received_gross,
            broker_paid_cents=broker_paid_by_project.get(int(p.id), 0),
        )

        # 已终止项目：计算分红数据
        div_paid = 0
        div_remaining = 0
        if p.status == "ended":
            dm = dividend_metrics(p.id, received_net_cents=int(fin["received_net_cents"]))
            div_paid = dm.paid_cents
            div_remaining = dm.remaining_cents

        row = {
            "project": p,
            "expected_total_cents": expected_total_cents,
            "expected_net_cents": fin["expected_net_cents"],
            "received_cents": fin["received_bank_income_cents"],
            "received_net_cents": fin["received_net_cents"],
            "remaining_gross_cents": fin["remaining_gross_cents"],
            "remaining_net_cents": fin["remaining_net_cents"],
            "remaining_net_cash_cents": fin["remaining_net_cash_cents"],
            "broker_unpaid_cents": fin["broker_unpaid_cents"],
            "finance": fin,
            "dividend_paid": div_paid,
            "dividend_remaining": div_remaining,
        }
        if p.status == "ended":
            ended_rows.append(row)
        else:
            active_rows.append(row)

    # 已终止项目：按"还有未分红"置顶
    ended_rows.sort(key=lambda r: (0 if r["dividend_remaining"] > 0 else 1, r["project"].id or 0), reverse=True)

    # 汇总已终止项目财务
    ended_total_income = sum(r["received_net_cents"] for r in ended_rows)
    ended_total_expense = sum(
        int(
            db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
            .filter(
                Transaction.project_id == r["project"].id,
                Transaction.type == TransactionType.EXPENSE.value,
                Transaction.settled.is_(True),
                Transaction.is_void.is_(False),
                Transaction.status == "active",
                ~db.func.coalesce(Transaction.note, "").like("[DIVIDEND]%"),
            )
            .scalar() or 0
        )
        for r in ended_rows
    )
    ended_total_profit = ended_total_income - ended_total_expense
    ended_total_div_paid = sum(r["dividend_paid"] for r in ended_rows)
    ended_total_div_remaining = sum(r["dividend_remaining"] for r in ended_rows)

    # chart: 按「我方未收净额」排序（仅进行中项目）
    top_projects = sorted(
        active_rows,
        key=lambda r: r["remaining_net_cash_cents"], reverse=True
    )[:10]

    # 构建子项目映射：{parent_id: [child_row, ...]}
    child_rows_map = {}
    for r in active_rows:
        p = r["project"]
        if p.parent_project_id:
            ppid = int(p.parent_project_id)
            child_rows_map.setdefault(ppid, []).append(r)
    chart_projects = {
        "labels": [r["project"].name for r in top_projects],
        "remaining": [int(r["remaining_net_cents"]) / 100 for r in top_projects],
        "remaining_cash": [int(r["remaining_net_cash_cents"]) / 100 for r in top_projects],
        "received": [int(r["received_net_cents"]) / 100 for r in top_projects],
    }

    list_per_page = 15
    list_page = request.args.get("page", 1, type=int) or 1
    list_page = max(list_page, 1)
    list_total = len(active_rows)
    list_pages = max((list_total + list_per_page - 1) // list_per_page, 1) if list_total else 1
    if list_page > list_pages:
        list_page = list_pages
    start = (list_page - 1) * list_per_page
    active_rows_page = active_rows[start : start + list_per_page]

    # 可分配盈余：全部项目利润合计（不含分红支出）
    surplus_cents = sum(
        r["received_net_cents"] - max(
            int(db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
               .filter(Transaction.project_id == r["project"].id, Transaction.type == "expense",
                       Transaction.settled.is_(True), Transaction.is_void.is_(False),
                       Transaction.status == "active")
               .scalar() or 0)
            - int(db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
                  .filter(Transaction.project_id == r["project"].id, Transaction.type == "expense",
                          Transaction.settled.is_(True), Transaction.is_void.is_(False),
                          Transaction.status == "active",
                          db.func.coalesce(Transaction.note, "").like("[DIVIDEND]%"))
                  .scalar() or 0), 0)
        for r in active_rows
    )

    return render_template(
        "dashboard.html",
        income_cents=int(income_cents),
        expense_cents=int(expense_cents),
        net_cents=int(income_cents) - int(expense_cents),
        surplus_cents=int(surplus_cents),
        project_rows=active_rows_page,
        list_page=list_page,
        list_pages=list_pages,
        list_total=list_total,
        list_has_prev=list_page > 1,
        list_has_next=list_page < list_pages,
        list_prev_page=list_page - 1,
        list_next_page=list_page + 1,
        chart_projects_json=json.dumps(chart_projects, ensure_ascii=False),
        active_count=active_count,
        ended_count=ended_count,
        ended_rows=ended_rows,
        child_rows_map=child_rows_map,
        ended_total_income=ended_total_income,
        ended_total_expense=ended_total_expense,
        ended_total_profit=ended_total_profit,
        ended_total_div_paid=ended_total_div_paid,
        ended_total_div_remaining=ended_total_div_remaining,
        is_admin=is_admin(),
    )


@bp.get("/reports")
@login_required
def reports():
    # date range defaults: last 30 days
    end = request.args.get("end")
    start = request.args.get("start")
    try:
        end_d = datetime.strptime(end, "%Y-%m-%d").date() if end else date.today()
    except ValueError:
        end_d = date.today()
    try:
        start_d = datetime.strptime(start, "%Y-%m-%d").date() if start else (end_d - timedelta(days=29))
    except ValueError:
        start_d = end_d - timedelta(days=29)

    # totals in range (occur_date)
    income_cents_q = db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0)).filter(
        Transaction.type == TransactionType.INCOME.value,
        Transaction.is_void.is_(False),
        Transaction.status == "active",
        Transaction.settled.is_(True),
        Transaction.project_id.isnot(None),
        Transaction.occur_date >= start_d,
        Transaction.occur_date <= end_d,
    )
    income_cents_q = _apply_transaction_project_access_filter(income_cents_q)
    income_cents = income_cents_q.scalar() or 0
    expense_cents_q = db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0)).filter(
        Transaction.type == TransactionType.EXPENSE.value,
        Transaction.is_void.is_(False),
        Transaction.status == "active",
        Transaction.settled.is_(True),
        Transaction.project_id.isnot(None),
        Transaction.occur_date >= start_d,
        Transaction.occur_date <= end_d,
    )
    expense_cents_q = _apply_transaction_project_access_filter(expense_cents_q)
    expense_cents = expense_cents_q.scalar() or 0

    # daily trend
    days = []
    cur = start_d
    while cur <= end_d:
        days.append(cur)
        cur += timedelta(days=1)

    income_by_day = {d.isoformat(): 0 for d in days}
    expense_by_day = {d.isoformat(): 0 for d in days}

    rows_q = db.session.query(
        Transaction.occur_date, Transaction.type, db.func.sum(Transaction.amount_cents)
    ).filter(
        Transaction.is_void.is_(False),
        Transaction.status == "active",
        Transaction.settled.is_(True),
        Transaction.project_id.isnot(None),
        Transaction.occur_date >= start_d,
        Transaction.occur_date <= end_d,
    )
    rows_q = _apply_transaction_project_access_filter(rows_q)
    rows = rows_q.group_by(Transaction.occur_date, Transaction.type).all()
    for d, ttype, s in rows:
        key = d.isoformat()
        if ttype == TransactionType.INCOME.value:
            income_by_day[key] = int(s or 0)
        elif ttype == TransactionType.EXPENSE.value:
            expense_by_day[key] = int(s or 0)

    trend = {
        "labels": [d.strftime("%m-%d") for d in days],
        "income": [income_by_day[d.isoformat()] / 100 for d in days],
        "expense": [expense_by_day[d.isoformat()] / 100 for d in days],
    }

    # top counterparties (expense)
    top_exp_q = db.session.query(
        db.func.coalesce(Transaction.counterparty, "（未填）").label("cp"),
        db.func.sum(Transaction.amount_cents).label("s"),
    ).filter(
        Transaction.type == TransactionType.EXPENSE.value,
        Transaction.is_void.is_(False),
        Transaction.status == "active",
        Transaction.settled.is_(True),
        Transaction.project_id.isnot(None),
        Transaction.occur_date >= start_d,
        Transaction.occur_date <= end_d,
    )
    top_exp_q = _apply_transaction_project_access_filter(top_exp_q)
    top_expense_counterparties = (
        top_exp_q.group_by("cp").order_by(db.desc("s")).limit(10).all()
    )
    pie = {
        "labels": [r.cp for r in top_expense_counterparties],
        "values": [int(r.s or 0) / 100 for r in top_expense_counterparties],
    }

    tx_page = request.args.get("tx_page", 1, type=int) or 1
    tx_page = max(tx_page, 1)
    tx_list_q = Transaction.query.options(
        joinedload(Transaction.project), joinedload(Transaction.attachments)
    ).filter(
        Transaction.is_void.is_(False),
        Transaction.status == "active",
        Transaction.settled.is_(True),
        Transaction.project_id.isnot(None),
        Transaction.occur_date >= start_d,
        Transaction.occur_date <= end_d,
    )
    tx_list_q = _apply_transaction_project_access_filter(tx_list_q)
    tx_pagination = tx_list_q.order_by(
        Transaction.occur_date.desc(), Transaction.id.desc()
    ).paginate(page=tx_page, per_page=30, error_out=False)

    return render_template(
        "reports.html",
        start=start_d.isoformat(),
        end=end_d.isoformat(),
        income_cents=int(income_cents),
        expense_cents=int(expense_cents),
        net_cents=int(income_cents) - int(expense_cents),
        trend_json=json.dumps(trend, ensure_ascii=False),
        expense_pie_json=json.dumps(pie, ensure_ascii=False),
        tx_pagination=tx_pagination,
        is_admin=is_admin(),
    )

