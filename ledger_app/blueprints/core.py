"""core.py：路由实现（原 routes.py 拆分而来，URL/行为不变）。"""
from ._common import *  # noqa: F401,F403

from ..services.payments import can_pay, finance_todos


@bp.before_request
def _load_pending_approvals():
    """每次请求只算一次待办，供导航铃铛 / 登录弹窗 / 待办页共用。"""
    if not getattr(current_user, "is_authenticated", False):
        return
    try:
        all_items = pending_items_for_user(
            int(current_user.id), is_admin_user=is_admin()
        )
    except Exception:  # pragma: no cover - 待办统计失败不应影响正常页面
        current_app.logger.exception("计算待处理事项失败")
        all_items = []
    g.pending_items = [i for i in all_items if not i.approved_by_me]
    g.pending_waiting = [i for i in all_items if i.approved_by_me]
    g.pending_count = len(g.pending_items)
    # 今天该写但还没写的日志（和待办一起在登录弹窗/待办页提醒）
    try:
        g.log_todo_items = pending_log_tasks_for_user(int(current_user.id))
    except Exception:  # pragma: no cover - 计算失败不应影响页面
        current_app.logger.exception("计算待写日志失败")
        g.log_todo_items = []
    g.log_todo_count = len(g.log_todo_items)
    # 付款闭环待办：待打款 / 待补回单 / 待确认到账
    try:
        pay_todos = finance_todos(
            user_id=int(current_user.id), finance=can_pay()
        )
    except Exception:  # pragma: no cover
        current_app.logger.exception("计算付款闭环待办失败")
        pay_todos = {"payments": [], "receipts": [], "income": []}
    g.pay_todos = pay_todos["payments"]
    g.receipt_todos = pay_todos["receipts"]
    g.income_todos = pay_todos["income"]
    g.pay_todo_count = (
        len(g.pay_todos) + len(g.receipt_todos) + len(g.income_todos)
    )
    # 登录后第一次打开页面时弹窗提醒；只弹一次（待办 或 待写日志 任一非空就弹）
    g.show_pending_modal = bool(session.pop("show_pending_modal", False)) and bool(
        g.pending_items or g.log_todo_items or g.pay_todo_count
    )


@bp.after_request
def _honor_next_redirect(response):
    """让「待办页」的表单在操作后跳回原页面（仅接受站内路径）。"""
    if response.status_code not in (301, 302, 303, 307, 308):
        return response
    target = (request.form.get("next") or "").strip()
    if (
        target.startswith("/")
        and not target.startswith("//")
        and "\n" not in target
        and "\r" not in target
    ):
        response.headers["Location"] = target
    return response


@bp.get("/approvals")
@login_required
def approvals_list():
    """待我处理：需要我同意的申请 + 我已同意但还在等别人的申请。"""
    return render_template(
        "approvals_list.html",
        todo_items=getattr(g, "pending_items", []),
        waiting_items=getattr(g, "pending_waiting", []),
        pending_count=getattr(g, "pending_count", 0),
        log_items=getattr(g, "log_todo_items", []),
        log_count=getattr(g, "log_todo_count", 0),
        pay_items=getattr(g, "pay_todos", []),
        receipt_items=getattr(g, "receipt_todos", []),
        income_items=getattr(g, "income_todos", []),
        pay_todo_count=getattr(g, "pay_todo_count", 0),
        is_admin=is_admin(),
    )


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
        # 下次打开页面时弹一次「待我处理」提醒
        session["show_pending_modal"] = True
        return redirect(url_for("main.dashboard"))

    return render_template("login.html", form=form)


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("main.login"))


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

