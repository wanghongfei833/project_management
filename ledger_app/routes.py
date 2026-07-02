from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json

from sqlalchemy.orm import joinedload

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from .extensions import db
from .project_finance import (
    BROKER_DIR_NET_FROM_BROKER,
    BROKER_DIR_WE_PAY,
    BROKER_MODE_FIXED,
    BROKER_MODE_PERCENT,
    build_project_finance,
    referral_ratio_dec,
    running_settled_cash_balance_by_transaction_id,
)
from .forms import (
    ChangePasswordForm,
    CP_EXPENSE_BROKER,
    CP_EXPENSE_MEMBER,
    CP_EXPENSE_OTHER,
    CP_EXPENSE_REFUND,
    CP_EXPENSE_REIMBURSE,
    CP_EXPENSE_SUPPLIER,
    CP_INCOME_OTHER,
    CP_INCOME_OWNER,
    EXPENSE_CP_CHOICES,
    INCOME_CP_CHOICES,
    LoginForm,
    ProjectAdjustmentForm,
    ProjectDividendForm,
    ProjectEndDateChangeForm,
    ProjectForm,
    ProjectUpdateForm,
    TransactionEditForm,
    TransactionForm,
    UserCreateForm,
    UserEditForm,
)
from .models import (
    Attachment,
    Project,
    ProjectActivityLog,
    ProjectAdjustmentApproval,
    ProjectExpectedIncomeAdjustment,
    ProjectMember,
    ProjectDeleteApproval,
    ProjectDeleteRequest,
    ProjectDividendDistribution,
    ProjectDividendRecipient,
    ProjectEndApproval,
    ProjectEndDateChangeApproval,
    ProjectEndDateChangeRequest,
    ProjectEndRequest,
    ProjectReviveApproval,
    ProjectReviveRequest,
    ProjectUpdate,
    ProjectUpdateAttachment,
    Role,
    Transaction,
    TransactionCreateApproval,
    TransactionCreateRequest,
    TransactionDeleteApproval,
    TransactionDeleteRequest,
    TransactionEditApproval,
    TransactionEditRequest,
    TransactionType,
    User,
)
from .upload_paths import (
    attachment_display_name,
    project_update_attachment_relpath,
    transaction_attachment_relpath,
)
from .utils import safe_join_upload, sha256_file

bp = Blueprint("main", __name__)

DEFAULT_NEW_USER_PASSWORD = "123456!"


def is_admin() -> bool:
    return getattr(current_user, "role", None) == Role.ADMIN.value


def _admin_user_ids() -> set[int]:
    rows = db.session.query(User.id).filter(User.role == Role.ADMIN.value).all()
    return {int(r[0]) for r in rows}


def _accessible_projects_query():
    """管理员可查全部项目；普通用户仅可查本人所在项目。"""
    if is_admin():
        return Project.query
    return (
        Project.query.join(ProjectMember, ProjectMember.project_id == Project.id)
        .filter(ProjectMember.user_id == current_user.id)
    )


def _accessible_member_project_ids_query():
    """非管理员：返回本人所在项目的 project_id 查询（单列）；管理员返回 None 表示不限制。"""
    if is_admin():
        return None
    return db.session.query(ProjectMember.project_id).filter(
        ProjectMember.user_id == current_user.id
    )


def _apply_transaction_project_access_filter(q):
    """将流水查询限制为当前用户可访问的项目（管理员不加限制）。"""
    ids_q = _accessible_member_project_ids_query()
    if ids_q is not None:
        q = q.filter(Transaction.project_id.in_(ids_q))
    return q


def yuan_to_cents(y) -> int:
    # Accept Decimal/float/str; store as integer cents.
    return int((Decimal(str(y)) * Decimal("100")).quantize(Decimal("1")))


def cents_to_yuan(cents: int) -> Decimal:
    return (Decimal(int(cents)) / Decimal("100")).quantize(Decimal("0.01"))


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


def _dividend_remaining_cents(project: Project) -> int:
    """计算项目的剩余可分红额度。"""
    pid = int(project.id)
    # 已结算收入
    received_cents = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(
            Transaction.project_id == pid,
            Transaction.type == TransactionType.INCOME.value,
            Transaction.settled.is_(True),
            Transaction.is_void.is_(False),
            Transaction.status == "active",
        )
        .scalar()
        or 0
    )
    # 已结算支出总额
    paid_cents_all = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(
            Transaction.project_id == pid,
            Transaction.type == TransactionType.EXPENSE.value,
            Transaction.settled.is_(True),
            Transaction.is_void.is_(False),
            Transaction.status == "active",
        )
        .scalar()
        or 0
    )
    # 分红支出
    dividend_expense_cents = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(
            Transaction.project_id == pid,
            Transaction.type == TransactionType.EXPENSE.value,
            Transaction.settled.is_(True),
            Transaction.is_void.is_(False),
            Transaction.status == "active",
            db.func.coalesce(Transaction.note, "").like("[DIVIDEND]%"),
        )
        .scalar()
        or 0
    )
    paid_cents_excl_dividend = int(paid_cents_all)
    expected_total_cents = project_expected_total_cents(pid)
    finance = build_project_finance(
        project,
        expected_total_cents=expected_total_cents,
        received_settled_income_bank_cents=int(received_cents),
    )
    received_net_cents = finance["received_net_cents"]
    dividend_base_cents = max(int(received_net_cents) - paid_cents_excl_dividend, 0)
    dividend_paid_total_cents = int(
        db.session.query(
            db.func.coalesce(db.func.sum(ProjectDividendDistribution.amount_cents), 0)
        )
        .filter(ProjectDividendDistribution.project_id == pid)
        .scalar()
        or 0
    )
    return max(dividend_base_cents - dividend_paid_total_cents, 0)


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


@bp.route("/me/password", methods=["GET", "POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("当前密码不正确", "danger")
            return render_template("change_password.html", form=form, is_admin=is_admin())

        if form.current_password.data == form.new_password.data:
            flash("新密码不能与当前密码相同", "warning")
            return render_template("change_password.html", form=form, is_admin=is_admin())

        current_user.set_password(form.new_password.data)
        db.session.commit()
        flash("密码已更新", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("change_password.html", form=form, is_admin=is_admin())


@bp.get("/users")
@login_required
def users_list():
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.dashboard"))

    users = User.query.order_by(User.id.asc()).all()
    return render_template("users_list.html", users=users, is_admin=is_admin())


@bp.route("/users/new", methods=["GET", "POST"])
@login_required
def users_new():
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.dashboard"))

    form = UserCreateForm()
    if form.validate_on_submit():
        if User.query.filter_by(username=form.username.data.strip()).first():
            flash("用户名已存在", "warning")
            return render_template("user_form.html", form=form, mode="create", is_admin=is_admin())

        pwd = (form.password.data or "").strip() or DEFAULT_NEW_USER_PASSWORD
        u = User(
            username=form.username.data.strip(),
            role=form.role.data,
            is_active=bool(form.is_active.data),
        )
        u.set_password(pwd)
        db.session.add(u)
        db.session.commit()
        if not (form.password.data or "").strip():
            flash(f"用户已创建，初始密码：{DEFAULT_NEW_USER_PASSWORD}", "success")
        else:
            flash("用户已创建（已使用你填写的初始密码）", "success")
        return redirect(url_for("main.users_list"))

    return render_template("user_form.html", form=form, mode="create", is_admin=is_admin())


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def users_edit(user_id: int):
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.dashboard"))

    u = db.session.get(User, user_id)
    if not u:
        flash("用户不存在", "warning")
        return redirect(url_for("main.users_list"))

    form = UserEditForm()
    if request.method == "GET":
        form.role.data = u.role
        form.is_active.data = bool(u.is_active)

    if form.validate_on_submit():
        if u.username == "admin" and form.role.data != Role.ADMIN.value:
            flash("不能将 admin 账号降级为非管理员", "warning")
            return render_template("user_edit.html", user=u, form=form, is_admin=is_admin())

        if u.id == current_user.id and not bool(form.is_active.data):
            flash("不能禁用当前登录账号", "warning")
            return render_template("user_edit.html", user=u, form=form, is_admin=is_admin())

        u.role = form.role.data
        u.is_active = bool(form.is_active.data)
        db.session.commit()
        flash("用户已更新", "success")
        return redirect(url_for("main.users_list"))

    return render_template("user_edit.html", user=u, form=form, is_admin=is_admin())


@bp.post("/users/<int:user_id>/reset-password")
@login_required
def users_reset_password(user_id: int):
    if not is_admin():
        return jsonify({"ok": False, "error": "无权限"}), 403

    u = db.session.get(User, user_id)
    if not u:
        return jsonify({"ok": False, "error": "用户不存在"}), 404

    admin_password = (request.form.get("admin_password") or "").strip()
    if not current_user.check_password(admin_password):
        return jsonify({"ok": False, "error": "管理员密码不正确"}), 400

    u.set_password(DEFAULT_NEW_USER_PASSWORD)
    # 重置密码时通常也希望恢复可登录状态（避免“重置成功但仍无法登录”）。
    u.is_active = True
    db.session.commit()
    return jsonify(
        {
            "ok": True,
            "username": u.username,
            "new_password": DEFAULT_NEW_USER_PASSWORD,
            "is_active": bool(u.is_active),
        }
    )


@bp.get("/health")
def health():
    return {"ok": True}


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if not user:
            flash("用户名或密码错误", "danger")
            return render_template("login.html", form=form)
        if not user.is_active:
            flash("用户名或密码错误", "danger")
            return render_template("login.html", form=form)
        if not user.check_password(form.password.data):
            flash("用户名或密码错误", "danger")
            return render_template("login.html", form=form)
        login_user(user)
        return redirect(url_for("main.dashboard"))

    return render_template("login.html", form=form)


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("main.login"))


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
        )

        # 已终止项目：计算分红数据
        div_paid = 0
        div_remaining = 0
        if p.status == "ended":
            paid_cents_all = int(
                db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
                .filter(
                    Transaction.project_id == p.id,
                    Transaction.type == TransactionType.EXPENSE.value,
                    Transaction.settled.is_(True),
                    Transaction.is_void.is_(False),
                    Transaction.status == "active",
                )
                .scalar() or 0
            )
            div_expense = int(
                db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
                .filter(
                    Transaction.project_id == p.id,
                    Transaction.type == TransactionType.EXPENSE.value,
                    Transaction.settled.is_(True),
                    Transaction.is_void.is_(False),
                    Transaction.status == "active",
                    db.func.coalesce(Transaction.note, "").like("[DIVIDEND]%"),
                )
                .scalar() or 0
            )
            paid_excl_div = max(paid_cents_all - div_expense, 0)
            div_base = max(int(fin["received_net_cents"]) - paid_excl_div, 0)
            div_paid = int(
                db.session.query(db.func.coalesce(db.func.sum(ProjectDividendDistribution.amount_cents), 0))
                .filter(ProjectDividendDistribution.project_id == p.id)
                .scalar() or 0
            )
            div_remaining = max(div_base - div_paid, 0)

        row = {
            "project": p,
            "expected_total_cents": expected_total_cents,
            "expected_net_cents": fin["expected_net_cents"],
            "received_cents": fin["received_bank_income_cents"],
            "received_net_cents": fin["received_net_cents"],
            "remaining_gross_cents": fin["remaining_gross_cents"],
            "remaining_net_cents": fin["remaining_net_cents"],
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
        key=lambda r: r["remaining_net_cents"], reverse=True
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
        if mode == BROKER_MODE_FIXED and direction == BROKER_DIR_WE_PAY and fixed_cents <= 0:
            flash("在「固定金额」且「我方另付介绍费」模式下，请填写大于 0 的中介固定费用。", "warning")
            return render_template("project_form.html", form=form, is_admin=is_admin(), parent_candidates=parent_candidates, admin_user_ids=admin_user_ids, parent_member_ids=parent_member_ids)
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
            status=form.status.data,
            note=form.note.data or None,
            parent_project_id=parent_id,
            can_dividend=can_div,
        )
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
        if mode == BROKER_MODE_FIXED and direction == BROKER_DIR_WE_PAY and fixed_cents <= 0:
            flash("在「固定金额」且「我方另付介绍费」模式下，请填写大于 0 的中介固定费用。", "warning")
            return render_template(
                "project_form.html",
                form=form,
                is_admin=is_admin(),
                mode="edit",
                project=p,
                parent_candidates=parent_candidates,
                admin_user_ids=admin_user_ids,
                parent_member_ids=parent_member_ids,
            )
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
        p.status = form.status.data
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


def _is_project_member(project_id: int, user_id: int) -> bool:
    return (
        ProjectMember.query.filter_by(project_id=project_id, user_id=user_id).first()
        is not None
    )


def _project_member_user_ids(project_id: int) -> set[int]:
    rows = ProjectMember.query.filter_by(project_id=project_id).all()
    return {int(r.user_id) for r in rows}


def _project_non_admin_member_user_ids(project_id: int) -> set[int]:
    """返回项目中去掉 admin 角色的成员 ID 集合（非 admin 成员全部同意即可通过审批，无需 admin 同意）。"""
    rows = (
        db.session.query(ProjectMember.user_id)
        .join(User, User.id == ProjectMember.user_id)
        .filter(
            ProjectMember.project_id == project_id,
            User.role != Role.ADMIN.value,
        )
        .all()
    )
    return {int(r.user_id) for r in rows}


def _check_project_not_ended(project) -> bool:
    """检查项目是否已终止，已终止则 flash 提示并返回 False。"""
    if getattr(project, "status", None) == "ended":
        flash("项目已终止，不可执行此操作", "danger")
        return False
    return True


def _open_transaction_edit_request(transaction_id: int) -> TransactionEditRequest | None:
    return (
        TransactionEditRequest.query.filter_by(
            transaction_id=transaction_id, status="open"
        )
        .order_by(TransactionEditRequest.id.desc())
        .first()
    )


def _open_transaction_delete_request(transaction_id: int) -> TransactionDeleteRequest | None:
    return (
        TransactionDeleteRequest.query.filter_by(
            transaction_id=transaction_id, status="open"
        )
        .order_by(TransactionDeleteRequest.id.desc())
        .first()
    )


def _transaction_delete_fully_approved(req: TransactionDeleteRequest) -> bool:
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _transaction_delete_can_execute(req: TransactionDeleteRequest) -> bool:
    # admin 绝对权力：可随时执行
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _transaction_delete_fully_approved(req)


def _can_view_project(user_id: int, project_id: int) -> bool:
    """用户是否能查看某个项目（穿透父项目链）。"""
    visited = set()
    p = db.session.get(Project, project_id)
    while p:
        if p.id in visited:
            break
        visited.add(p.id)
        if _is_project_member(p.id, user_id):
            return True
        p = p.parent_project
    return False


def _set_counterparty_choices(form, project_id):
    """设置对方下拉选项及项目人员列表。"""
    from .forms import EXPENSE_CP_CHOICES, INCOME_CP_CHOICES
    # 包含全部选项，前端 JS 按类型切换显示
    form.counterparty.choices = INCOME_CP_CHOICES + EXPENSE_CP_CHOICES

    # 项目人员下拉
    members = []
    if project_id:
        members = (
            db.session.query(User)
            .join(ProjectMember, ProjectMember.user_id == User.id)
            .filter(ProjectMember.project_id == int(project_id))
            .order_by(User.username.asc())
            .all()
        )
    form.recipient_user_id.choices = [(0, "（不指定）")] + [(u.id, u.username) for u in members]


def _collect_all_sub_project_ids(project_id: int) -> set[int]:
    """递归收集所有子孙项目的 ID。"""
    ids = {project_id}
    for child in Project.query.filter_by(parent_project_id=project_id).all():
        ids |= _collect_all_sub_project_ids(int(child.id))
    return ids


def _open_transaction_create_request(transaction_id: int) -> TransactionCreateRequest | None:
    return (
        TransactionCreateRequest.query.filter_by(
            transaction_id=transaction_id, status="open"
        )
        .order_by(TransactionCreateRequest.id.desc())
        .first()
    )


def _transaction_create_fully_approved(req: TransactionCreateRequest) -> bool:
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _transaction_create_can_execute(req: TransactionCreateRequest) -> bool:
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _transaction_create_fully_approved(req)


def _approval_user_ids_include_any_admin(user_ids: set[int]) -> bool:
    """审批记录里是否包含至少一名管理员账号（用于「全员同意 或 管理员同意」）。"""
    if not user_ids:
        return False
    return (
        db.session.query(User.id)
        .filter(User.id.in_(user_ids), User.role == Role.ADMIN.value)
        .first()
        is not None
    )


def _transaction_edit_fully_approved(req: TransactionEditRequest) -> bool:
    """项目全部非 admin 成员已在该修改申请上同意。"""
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _transaction_edit_can_execute(req: TransactionEditRequest) -> bool:
    """审核通过：全体成员同意 或 管理员同意；管理员可随时执行（绝对权力）。"""
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _transaction_edit_fully_approved(req)


MAX_ATTACHMENTS_PER_TX = 10


def _save_transaction_attachments(tx: Transaction) -> int:
    """保存本次请求中上传的流水凭证。返回成功保存的文件数。"""
    if not tx.project_id:
        return 0
    project_id = int(tx.project_id)
    upload_root = current_app.config["UPLOAD_FOLDER"]
    files = request.files.getlist("attachments")

    # 计算已有附件数
    existing_count = Attachment.query.filter_by(transaction_id=tx.id).count()
    if existing_count + len([f for f in files if f and getattr(f, "filename", "")]) > MAX_ATTACHMENTS_PER_TX:
        flash(f"每笔流水最多 {MAX_ATTACHMENTS_PER_TX} 个附件", "warning")
        return 0
    n = 0
    for f in files:
        if not f or not getattr(f, "filename", ""):
            continue
        display_name = attachment_display_name(f.filename)

        # 校验文件后缀
        try:
            from .upload_paths import attachment_disk_suffix
            attachment_disk_suffix(display_name)
        except ValueError as e:
            flash(str(e), "warning")
            continue

        f.stream.seek(0)
        digest = sha256_file(f.stream)
        f.stream.seek(0)

        stored_rel = transaction_attachment_relpath(
            project_id, int(tx.id), digest, display_name
        )
        abs_path = safe_join_upload(upload_root, stored_rel)
        Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
        f.save(abs_path)

        db.session.add(
            Attachment(
                transaction_id=tx.id,
                filename=display_name,
                stored_path=stored_rel,
                sha256=digest,
                uploaded_by_user_id=current_user.id,
            )
        )
        n += 1
    return n


def _apply_transaction_attachment_deletes(tx: Transaction, id_strs: list[str]) -> int:
    """按附件 ID 删除流水凭证（磁盘 + 数据库）。仅允许删除属于该流水的附件。返回删除条数。"""
    upload_root = current_app.config["UPLOAD_FOLDER"]
    n = 0
    for raw in id_strs:
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        att = db.session.get(Attachment, aid)
        if not att or int(att.transaction_id) != int(tx.id):
            continue
        try:
            Path(safe_join_upload(upload_root, att.stored_path)).unlink(missing_ok=True)
        except Exception:
            pass
        db.session.delete(att)
        n += 1
    return n


def _open_project_end_date_change_request(project_id: int) -> ProjectEndDateChangeRequest | None:
    return (
        ProjectEndDateChangeRequest.query.filter_by(project_id=project_id, status="open")
        .order_by(ProjectEndDateChangeRequest.id.desc())
        .first()
    )


def _project_end_date_change_fully_approved(req: ProjectEndDateChangeRequest) -> bool:
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _project_end_date_change_can_execute(req: ProjectEndDateChangeRequest | None) -> bool:
    if not req:
        return False
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _project_end_date_change_fully_approved(req)


def _save_project_update_attachments(update: ProjectUpdate) -> int:
    if not update.project_id:
        return 0
    project_id = int(update.project_id)
    upload_root = current_app.config["UPLOAD_FOLDER"]
    files = request.files.getlist("attachments")

    # 计算已有附件数
    existing_count = ProjectUpdateAttachment.query.filter_by(update_id=update.id).count()
    if existing_count + len([f for f in files if f and getattr(f, "filename", "")]) > MAX_ATTACHMENTS_PER_TX:
        flash(f"每条进展最多 {MAX_ATTACHMENTS_PER_TX} 个附件", "warning")
        return 0
    n = 0
    for f in files:
        if not f or not getattr(f, "filename", ""):
            continue
        display_name = attachment_display_name(f.filename)

        # 校验文件后缀
        try:
            from .upload_paths import attachment_disk_suffix
            attachment_disk_suffix(display_name)
        except ValueError as e:
            flash(str(e), "warning")
            continue

        f.stream.seek(0)
        digest = sha256_file(f.stream)
        f.stream.seek(0)

        stored_rel = project_update_attachment_relpath(
            project_id, int(update.id), digest, display_name
        )
        abs_path = safe_join_upload(upload_root, stored_rel)
        Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
        f.save(abs_path)

        db.session.add(
            ProjectUpdateAttachment(
                update_id=update.id,
                filename=display_name,
                stored_path=stored_rel,
                sha256=digest,
                uploaded_by_user_id=current_user.id,
            )
        )
        n += 1
    return n


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


def _open_project_end_request(project_id: int) -> ProjectEndRequest | None:
    return (
        ProjectEndRequest.query.filter_by(project_id=project_id, status="open")
        .order_by(ProjectEndRequest.id.desc())
        .first()
    )


def _project_end_fully_approved(req: ProjectEndRequest) -> bool:
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _project_end_can_execute(req: ProjectEndRequest | None) -> bool:
    if not req:
        return False
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _project_end_fully_approved(req)


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


def _open_project_revive_request(project_id: int) -> ProjectReviveRequest | None:
    return (
        ProjectReviveRequest.query.filter_by(project_id=project_id, status="open")
        .order_by(ProjectReviveRequest.id.desc())
        .first()
    )


def _project_revive_fully_approved(req: ProjectReviveRequest) -> bool:
    member_ids = _project_non_admin_member_user_ids(req.project_id)
    approved_ids = {a.user_id for a in req.approvals}
    return bool(member_ids) and member_ids.issubset(approved_ids)


def _project_revive_can_execute(req: ProjectReviveRequest | None) -> bool:
    if not req:
        return False
    if is_admin():
        return True
    approved_ids = {a.user_id for a in req.approvals}
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return _project_revive_fully_approved(req)


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


@bp.route("/projects/<int:project_id>/progress", methods=["GET"])
@login_required
def project_progress(project_id: int):
    p = db.session.get(Project, project_id)
    if not p:
        flash("项目不存在", "warning")
        return redirect(url_for("main.projects_list"))
    if not is_admin() and not _can_view_project(int(current_user.id), project_id):
        flash("无权查看", "danger")
        return redirect(url_for("main.projects_list"))

    page = request.args.get("page", 1, type=int) or 1
    page = max(page, 1)
    pagination = (
        ProjectUpdate.query.options(joinedload(ProjectUpdate.attachments), joinedload(ProjectUpdate.author))
        .filter_by(project_id=p.id)
        .order_by(ProjectUpdate.created_at.desc())
        .paginate(page=page, per_page=15, error_out=False)
    )
    can_post = p.status != "ended" and _is_project_member(p.id, current_user.id)
    return render_template("project_progress.html",
                          project=p, updates=pagination, can_post=can_post,
                          form=ProjectUpdateForm(), is_admin=is_admin())


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
    body_text = (u.body or "").strip()
    preview = body_text.replace("\n", " ")[:80]
    _log_project_activity(
        int(p.id),
        "project.update_note",
        f"提交进展记录（进展 id={u.id}，附件 {n_att} 个）：{preview}{'…' if len(body_text) > 80 else ''}",
    )
    db.session.commit()
    flash("进展已提交", "success")
    return redirect(url_for("main.project_progress", project_id=p.id))


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
    paid_cents_all = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(Transaction.project_id == p.id, Transaction.type == TransactionType.EXPENSE.value,
                Transaction.settled.is_(True), Transaction.is_void.is_(False), Transaction.status == "active")
        .scalar() or 0
    )
    div_expense = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(Transaction.project_id == p.id, Transaction.type == TransactionType.EXPENSE.value,
                Transaction.settled.is_(True), Transaction.is_void.is_(False), Transaction.status == "active",
                db.func.coalesce(Transaction.note, "").like("[DIVIDEND]%"))
        .scalar() or 0
    )
    paid_excl_div = paid_cents_all
    exp_total = project_expected_total_cents(p.id)
    fin = build_project_finance(p, expected_total_cents=exp_total, received_settled_income_bank_cents=received_cents)
    received_net = fin["received_net_cents"]
    div_base = max(received_net - paid_excl_div, 0)
    div_paid = int(
        db.session.query(db.func.coalesce(db.func.sum(ProjectDividendDistribution.amount_cents), 0))
        .filter(ProjectDividendDistribution.project_id == p.id).scalar() or 0
    )
    div_remaining = max(div_base - div_paid, 0)

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
        "paid_cents_excl_div": paid_excl_div,
        "dividend_base_cents": div_base,
        "dividend_paid_cents": div_paid,
        "dividend_remaining_cents": div_remaining,
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
    paid_cents_all = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(Transaction.project_id == p.id, Transaction.type == TransactionType.EXPENSE.value,
                Transaction.settled.is_(True), Transaction.is_void.is_(False), Transaction.status == "active")
        .scalar() or 0
    )
    div_expense = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(Transaction.project_id == p.id, Transaction.type == TransactionType.EXPENSE.value,
                Transaction.settled.is_(True), Transaction.is_void.is_(False), Transaction.status == "active",
                db.func.coalesce(Transaction.note, "").like("[DIVIDEND]%"))
        .scalar() or 0
    )
    paid_excl_div = paid_cents_all
    fin = build_project_finance(p, expected_total_cents=project_expected_total_cents(p.id),
                                received_settled_income_bank_cents=received_cents)
    div_base = max(int(fin["received_net_cents"]) - paid_excl_div, 0)
    div_paid = int(
        db.session.query(db.func.coalesce(db.func.sum(ProjectDividendDistribution.amount_cents), 0))
        .filter(ProjectDividendDistribution.project_id == p.id).scalar() or 0
    )
    div_remaining = max(div_base - div_paid, 0)

    if total_amt > div_remaining:
        flash(f"分红总额超出剩余可分红额度（剩余 ¥{cents_to_yuan(div_remaining)}）", "danger")
        return redirect(url_for("main.project_dividend_page", project_id=p.id))

    # 二次校验：在事务中重新确认剩余可分红（防并发超分）
    latest_div_paid = int(
        db.session.query(db.func.coalesce(db.func.sum(ProjectDividendDistribution.amount_cents), 0))
        .filter(ProjectDividendDistribution.project_id == p.id).scalar() or 0
    )
    latest_div_remaining = max(div_base - latest_div_paid, 0)
    if total_amt > latest_div_remaining:
        flash(f"分红额度已发生变化（剩余 ¥{cents_to_yuan(latest_div_remaining)}），请重新提交", "danger")
        return redirect(url_for("main.project_dividend_page", project_id=p.id))

    # 校验现金结余
    income_settled = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(Transaction.project_id == p.id, Transaction.type == TransactionType.INCOME.value,
                Transaction.settled.is_(True), Transaction.is_void.is_(False)).scalar() or 0
    )
    expense_settled = int(
        db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
        .filter(Transaction.project_id == p.id, Transaction.type == TransactionType.EXPENSE.value,
                Transaction.settled.is_(True), Transaction.is_void.is_(False)).scalar() or 0
    )
    cash_balance = income_settled - expense_settled
    if total_amt > cash_balance:
        flash(f"分红金额超出项目当前可用结余（结余 ¥{cents_to_yuan(cash_balance)}）", "danger")
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
        f"批量分红 ¥{cents_to_yuan(int(total_amt))} → {member_names}",
        detail=f"transaction_id={tx.id}",
    )
    db.session.commit()
    flash(f"分红完成：¥{cents_to_yuan(int(total_amt))} 分给 {len(dividend_items)} 人", "success")
    return redirect(url_for("main.project_dividend_page", project_id=p.id))


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
    if is_parent:
        expected_total_cents = sum(project_expected_total_cents(sid) for sid in all_ids)
        finance = build_project_finance(p, expected_total_cents=expected_total_cents,
                                        received_settled_income_bank_cents=int(received_cents))
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
        )
        balance_after_by_tx_id = running_settled_cash_balance_by_transaction_id(p.id)
    expected_net_cents = finance["expected_net_cents"]
    received_net_cents = finance["received_net_cents"]
    remaining_gross_cents = finance["remaining_gross_cents"]
    remaining_net_cents = finance["remaining_net_cents"]
    broker_fee_expected_total_cents = finance["broker_fee_expected_total_cents"]
    broker_estimated_cents = finance["broker_estimated_on_received_cents"]

    profit_realized_cents = int(received_net_cents) - int(paid_cents)
    profit_if_fully_collected_cents = int(expected_net_cents) - int(paid_cents)
    dividend_base_cents = max(int(received_net_cents) - int(paid_cents), 0)
    dividend_paid_total_cents = int(
        db.session.query(
            db.func.coalesce(db.func.sum(ProjectDividendDistribution.amount_cents), 0)
        )
        .filter(ProjectDividendDistribution.project_id == p.id)
        .scalar()
        or 0
    )
    dividend_remaining_cents = max(
        dividend_base_cents - dividend_paid_total_cents, 0
    )
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
        broker_fee_expected_total_cents=int(broker_fee_expected_total_cents),
        broker_estimated_cents=int(broker_estimated_cents),
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
                f"发起新增流水 #{tx.id} 审批：{typ} ¥{cents_to_yuan(int(tx.amount_cents))}，日期 {tx.occur_date}"
                + (f"，附件 {added} 个" if added else ""), detail=f"request_id={creq.id}")
        else:
            _log_project_activity(pid, "transaction.create",
                f"新增流水 #{tx.id}：{typ} ¥{cents_to_yuan(int(tx.amount_cents))}，日期 {tx.occur_date}"
                + (f"，附件 {added} 个" if added else ""))
        db.session.commit()
        if tx_status == "pending":
            flash("已提交新增流水申请，待审核通过后生效。", "success")
        else:
            flash(f"流水已新增{'，已保存 ' + str(added) + ' 个凭证文件' if added else ''}", "success")
        return redirect(url_for("main.project_detail", project_id=pid))
    return render_template("transaction_form.html", form=form, is_admin=is_admin())


@bp.get("/earnings")
@login_required
def personal_earnings():
    """个人收入查看：admin 看全部，普通用户只看自己。"""
    is_admin_user = is_admin()
    target_user_id = request.args.get("user_id", type=int)
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
                    Transaction.status == "active", Transaction.type == "expense")
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
            if not p: continue
            pd.append({"project_name": p.name, "payment": vals["payment"], "dividend": vals["dividend"]})
            tp += vals["payment"]; td += vals["dividend"]
        rows.append({"user": u, "projects": pd, "total_payment": tp, "total_dividend": td, "grand_total": tp + td})
    return render_template("personal_earnings.html", rows=rows, is_admin=is_admin_user)


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
            f"发起流水 #{tx.id} 修改申请 → {ntyp} ¥{cents_to_yuan(int(req.new_amount_cents))}，{req.new_occur_date}"
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
                f"修改自动执行（流水 #{tx.id}）：{ntyp} ¥{cents_to_yuan(int(req.new_amount_cents))}",
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
            f"修改自动执行（流水 #{tx.id}）：{ntyp} ¥{cents_to_yuan(int(req.new_amount_cents))}，{req.new_occur_date}",
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
        f"管理员执行流水 #{tx.id} 修改：{ntyp} ¥{cents_to_yuan(int(req.new_amount_cents))}，{req.new_occur_date}",
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
                f"发起追加应收 ¥{cents_to_yuan(int(adj.amount_cents))} 申请"
                + (f"（{adj.note}）" if adj.note else ""),
                detail=f"adjustment_id={adj.id}",
            )
            db.session.commit()
            flash("已提交追加应收申请，待审核通过后生效", "success")
        else:
            _log_project_activity(
                int(p.id),
                "project.adjust_expected",
                f"追加应收 ¥{cents_to_yuan(int(adj.amount_cents))}"
                + (f"（{adj.note}）" if adj.note else ""),
                detail=f"adjustment_id={adj.id}",
            )
            db.session.commit()
            flash("已追加应收款项", "success")
        return redirect(url_for("main.project_detail", project_id=p.id))

    return render_template("project_adjust.html", project=p, form=form, is_admin=is_admin())


def _adjustment_can_execute(adj: ProjectExpectedIncomeAdjustment) -> bool:
    """应收追加审核：admin绝对权力 / 有admin已同意 / 非admin成员全部同意。"""
    if is_admin():
        return True
    member_ids = _project_non_admin_member_user_ids(int(adj.project_id))
    approved_ids = {a.user_id for a in adj.approvals}
    if member_ids and member_ids.issubset(approved_ids):
        return True
    if _approval_user_ids_include_any_admin(approved_ids):
        return True
    return False


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
            f"追加应收自动执行：¥{cents_to_yuan(int(adj.amount_cents))}"
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
        f"取消追加应收 ¥{cents_to_yuan(int(adj.amount_cents))} 申请",
    )
    db.session.commit()
    flash("已取消追加应收申请", "success")
    return redirect(url_for("main.project_detail", project_id=p.id))


@bp.get("/uploads/<path:filename>")
@login_required
def uploads(filename: str):
    if is_admin():
        return send_from_directory(
            current_app.config["UPLOAD_FOLDER"], filename, as_attachment=False
        )

    att = Attachment.query.filter_by(stored_path=filename).first()
    if att:
        tx = db.session.get(Transaction, att.transaction_id)
        if (
            tx
            and tx.project_id
            and _is_project_member(int(tx.project_id), current_user.id)
        ):
            return send_from_directory(
                current_app.config["UPLOAD_FOLDER"], filename, as_attachment=False
            )
        abort(403)

    pua = ProjectUpdateAttachment.query.filter_by(stored_path=filename).first()
    if pua:
        upd = db.session.get(ProjectUpdate, pua.update_id)
        if upd and _is_project_member(int(upd.project_id), current_user.id):
            return send_from_directory(
                current_app.config["UPLOAD_FOLDER"], filename, as_attachment=False
            )
        abort(403)

    abort(403)

