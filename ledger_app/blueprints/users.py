"""users.py：路由实现（原 routes.py 拆分而来，URL/行为不变）。"""
from ._common import *  # noqa: F401,F403



@bp.get("/users")
@login_required
def users_list():
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.dashboard"))

    q_text = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()  # active / inactive
    phone_filter = (request.args.get("phone") or "").strip()  # set / empty

    query = User.query
    if q_text:
        like = f"%{q_text}%"
        query = query.filter(db.or_(User.username.like(like), User.phone.like(like)))
    if status == "active":
        query = query.filter(User.is_active.is_(True))
    elif status == "inactive":
        query = query.filter(User.is_active.is_(False))
    if phone_filter == "set":
        query = query.filter(User.phone.isnot(None), User.phone != "")
    elif phone_filter == "empty":
        query = query.filter(db.or_(User.phone.is_(None), User.phone == ""))

    users = query.order_by(User.id.asc()).all()
    all_users = User.query.all()
    stats = {
        "total": len(all_users),
        "active": sum(1 for u in all_users if u.is_active),
        "inactive": sum(1 for u in all_users if not u.is_active),
        "no_phone": sum(1 for u in all_users if not (u.phone or "").strip()),
    }
    return render_template(
        "users_list.html",
        users=users,
        stats=stats,
        q=q_text,
        selected_status=status,
        selected_phone=phone_filter,
        is_admin=is_admin(),
    )


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

        phone = normalize_phone(form.phone.data) or None
        if phone and User.query.filter_by(phone=phone).first():
            flash("该手机号已被其他用户使用", "warning")
            return render_template("user_form.html", form=form, mode="create", is_admin=is_admin())

        pwd = (form.password.data or "").strip() or DEFAULT_NEW_USER_PASSWORD
        u = User(
            username=form.username.data.strip(),
            phone=phone,
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
        if not phone:
            flash("该用户未填手机号，不会收到短信提醒（可随时在编辑页补录）", "warning")
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
        form.phone.data = u.phone or ""
        form.role.data = u.role
        form.is_active.data = bool(u.is_active)
        form.is_finance.data = bool(u.is_finance)

    if form.validate_on_submit():
        if u.username == "admin" and form.role.data != Role.ADMIN.value:
            flash("不能将 admin 账号降级为非管理员", "warning")
            return render_template("user_edit.html", user=u, form=form, is_admin=is_admin())

        if u.id == current_user.id and not bool(form.is_active.data):
            flash("不能禁用当前登录账号", "warning")
            return render_template("user_edit.html", user=u, form=form, is_admin=is_admin())

        phone = normalize_phone(form.phone.data) or None
        exists = (
            User.query.filter(User.phone == phone, User.id != u.id).first()
            if phone
            else None
        )
        if exists:
            flash(f"该手机号已被用户「{exists.username}」使用", "warning")
            return render_template("user_edit.html", user=u, form=form, is_admin=is_admin())

        u.phone = phone
        u.role = form.role.data
        u.is_active = bool(form.is_active.data)
        u.is_finance = bool(form.is_finance.data)
        db.session.commit()
        flash("用户已更新", "success")
        return redirect(url_for("main.users_list"))

    return render_template("user_edit.html", user=u, form=form, is_admin=is_admin())


def _purge_user_references(u: User) -> dict[str, int]:
    """删除用户前，清理所有指向 users.id 的外键引用。

    - 外键列是主键或 NOT NULL（如 project_members.user_id、各种 approval.user_id）→ 删除这些行；
    - 其它可空列（created_by / leader / recipient / actor 等）→ 置空，保留历史记录。
    """
    stats = {"deleted": 0, "nulled": 0}
    target = int(u.id)
    for table in db.metadata.sorted_tables:
        if table.name == "users":
            continue
        for fk in list(table.foreign_keys):
            if fk.column.table.name != "users" or fk.column.name != "id":
                continue
            col = fk.parent
            if col.primary_key or not col.nullable:
                res = db.session.execute(table.delete().where(col == target))
                stats["deleted"] += int(res.rowcount or 0)
            else:
                res = db.session.execute(
                    table.update().where(col == target).values(**{col.name: None})
                )
                stats["nulled"] += int(res.rowcount or 0)
    return stats


@bp.post("/users/<int:user_id>/delete")
@login_required
def users_delete(user_id: int):
    """删除用户（连带清理其成员关系与审批记录；历史流水的「发起人」置空保留）。"""
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.dashboard"))

    u = db.session.get(User, user_id)
    if not u:
        flash("用户不存在", "warning")
        return redirect(url_for("main.users_list"))
    if int(u.id) == int(current_user.id):
        flash("不能删除当前登录账号", "warning")
        return redirect(url_for("main.users_list"))
    if u.username == "admin":
        flash("不能删除内置 admin 账号", "warning")
        return redirect(url_for("main.users_list"))
    if u.role == Role.ADMIN.value:
        others = (
            db.session.query(db.func.count(User.id))
            .filter(User.role == Role.ADMIN.value, User.id != u.id)
            .scalar()
            or 0
        )
        if int(others) == 0:
            flash("至少需要保留一个管理员账号，无法删除", "warning")
            return redirect(url_for("main.users_list"))

    username = u.username
    stats = _purge_user_references(u)
    db.session.delete(u)
    db.session.commit()
    flash(
        f"已删除用户「{username}」"
        f"（清理关联记录 {stats['deleted']} 条，历史引用置空 {stats['nulled']} 处）",
        "success",
    )
    return redirect(url_for("main.users_list"))


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

