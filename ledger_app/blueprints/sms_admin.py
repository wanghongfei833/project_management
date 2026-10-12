"""sms_admin.py：路由实现（原 routes.py 拆分而来，URL/行为不变）。"""
from ._common import *  # noqa: F401,F403



def _sms_date_bounds():
    """解析短信记录的日期区间（左闭右开，按 naive UTC 比较）。"""
    start_d = _parse_iso_date(request.args.get("start"))
    end_d = _parse_iso_date(request.args.get("end"))
    lo = datetime.combine(start_d, datetime.min.time()) if start_d else None
    hi = (
        datetime.combine(end_d + timedelta(days=1), datetime.min.time())
        if end_d
        else None
    )
    return start_d, end_d, lo, hi


def _apply_sms_filters(q, *, user_id, project_id, status, lo, hi):
    if user_id:
        q = q.filter(SmsNotification.user_id == int(user_id))
    if status:
        q = q.filter(SmsNotification.status == status)
    if project_id:
        q = q.filter(
            SmsNotification.projects.any(
                SmsNotificationProject.project_id == int(project_id)
            )
        )
    if lo is not None:
        q = q.filter(SmsNotification.created_at >= lo)
    if hi is not None:
        q = q.filter(SmsNotification.created_at < hi)
    return q


@bp.get("/admin/sms")
@login_required
def sms_list():
    """短信溯源：发送记录 + 按项目统计 + 某项目发给谁。"""
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.dashboard"))

    user_id = request.args.get("user_id", type=int)
    project_id = request.args.get("project_id", type=int)
    status = (request.args.get("status") or "").strip()
    start_d, end_d, lo, hi = _sms_date_bounds()
    filters = dict(user_id=user_id, project_id=project_id, status=status, lo=lo, hi=hi)

    page = max(request.args.get("page", 1, type=int) or 1, 1)
    pagination = (
        _apply_sms_filters(
            SmsNotification.query.options(joinedload(SmsNotification.projects)),
            **filters,
        )
        .order_by(SmsNotification.created_at.desc(), SmsNotification.id.desc())
        .paginate(page=page, per_page=30, error_out=False)
    )

    def _count(**overrides):
        f = dict(filters)
        f.update(overrides)
        q = _apply_sms_filters(db.session.query(db.func.count(SmsNotification.id)), **f)
        return int(q.scalar() or 0)

    stats = {
        "all": _count(),
        "sent": _count(status="sent"),
        "failed": _count(status="failed"),
        "test": int(
            _apply_sms_filters(
                db.session.query(db.func.count(SmsNotification.id)).filter(
                    SmsNotification.trigger == "test"
                ),
                **filters,
            ).scalar()
            or 0
        ),
    }

    # 按项目统计（只算成功发送）
    proj_q = (
        db.session.query(
            SmsNotificationProject.project_id,
            SmsNotificationProject.project_name,
            db.func.count(db.func.distinct(SmsNotificationProject.notification_id)),
            db.func.count(db.func.distinct(SmsNotification.user_id)),
            db.func.sum(SmsNotificationProject.item_count),
            db.func.max(SmsNotification.created_at),
        )
        .join(
            SmsNotification,
            SmsNotification.id == SmsNotificationProject.notification_id,
        )
        .filter(SmsNotification.status == "sent")
    )
    if user_id:
        proj_q = proj_q.filter(SmsNotification.user_id == int(user_id))
    if lo is not None:
        proj_q = proj_q.filter(SmsNotification.created_at >= lo)
    if hi is not None:
        proj_q = proj_q.filter(SmsNotification.created_at < hi)
    project_rows = sorted(
        proj_q.group_by(
            SmsNotificationProject.project_id, SmsNotificationProject.project_name
        ).all(),
        key=lambda r: (-(int(r[2] or 0)), r[1] or ""),
    )

    # 指定项目：发给谁、各多少条
    recipient_rows = []
    if project_id:
        rec_q = (
            db.session.query(
                SmsNotification.user_id,
                SmsNotification.username,
                SmsNotification.phone,
                db.func.count(db.func.distinct(SmsNotification.id)),
                db.func.sum(SmsNotificationProject.item_count),
                db.func.max(SmsNotification.created_at),
            )
            .join(
                SmsNotificationProject,
                SmsNotificationProject.notification_id == SmsNotification.id,
            )
            .filter(
                SmsNotificationProject.project_id == int(project_id),
                SmsNotification.status == "sent",
            )
        )
        if lo is not None:
            rec_q = rec_q.filter(SmsNotification.created_at >= lo)
        if hi is not None:
            rec_q = rec_q.filter(SmsNotification.created_at < hi)
        recipient_rows = sorted(
            rec_q.group_by(
                SmsNotification.user_id,
                SmsNotification.username,
                SmsNotification.phone,
            ).all(),
            key=lambda r: (-(int(r[3] or 0)), r[1] or ""),
        )

    return render_template(
        "sms_list.html",
        pagination=pagination,
        records=pagination.items,
        stats=stats,
        project_rows=project_rows,
        recipient_rows=recipient_rows,
        status_info=sms_status(),
        users=User.query.order_by(User.username.asc()).all(),
        projects=Project.query.order_by(Project.name.asc()).all(),
        selected_user_id=user_id,
        selected_project_id=project_id,
        selected_status=status,
        start=start_d.isoformat() if start_d else "",
        end=end_d.isoformat() if end_d else "",
        is_admin=is_admin(),
    )


@bp.post("/admin/sms/run")
@login_required
def sms_run_now():
    """手动跑一轮待办检查（force=1 时忽略 6 小时冷却）。"""
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.dashboard"))

    force = bool(request.form.get("force"))
    result = send_pending_alerts(force=force, trigger="manual")
    skipped = result["skipped"]

    if not result["configured"]:
        flash(
            "短信未配置：请先设置环境变量 ALIYUN_SMS_ACCESS_KEY_ID / "
            "ALIYUN_SMS_ACCESS_KEY_SECRET / ALIYUN_SMS_SIGN_NAME",
            "warning",
        )
    elif result["sent"] or result["failed"]:
        flash(
            f"本轮检查完成：成功 {result['sent']} 条、失败 {result['failed']} 条",
            "success" if not result["failed"] else "warning",
        )
    else:
        flash(
            "本轮没有需要发送的提醒："
            f"待办未满 6 小时 {skipped['not_overdue']} 人、"
            f"6 小时内已发过 {skipped['cooling']} 人、"
            f"当前无待办 {skipped['no_pending']} 人、"
            f"未填手机号 {skipped['no_phone']} 人、"
            f"不在测试白名单 {skipped['not_allowed']} 人",
            "info",
        )
    return redirect(url_for("main.sms_list"))


@bp.post("/admin/sms/sync-status")
@login_required
def sms_sync_status():
    """同步最近 3 天的投递回执（区分「阿里云受理」与「用户真收到」）。"""
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.dashboard"))

    try:
        res = sync_delivery_status(days=3)
    except Exception as exc:  # pragma: no cover
        current_app.logger.exception("同步短信投递状态失败")
        flash(f"同步失败：{type(exc).__name__}: {exc}", "danger")
        return redirect(url_for("main.sms_list"))

    if not res["checked"]:
        flash("最近 3 天没有需要同步的短信", "info")
    else:
        msg = (
            f"已同步 {res['checked']} 组：投递成功 {res['delivered']} 条、"
            f"投递失败 {res['failed']} 条、仍在等待回执 {res['pending']} 条"
        )
        flash(msg, "warning" if res["failed"] else "success")
    for err in res.get("errors", [])[:3]:
        flash(f"查询异常：{err}", "warning")
    return redirect(url_for("main.sms_list"))


@bp.post("/admin/sms/test")
@login_required
def sms_send_test():
    """给指定号码发一条测试短信。"""
    if not is_admin():
        flash("无权限", "danger")
        return redirect(url_for("main.dashboard"))

    phone = (request.form.get("phone") or "").strip()
    number = request.form.get("number", 1, type=int) or 1
    if not is_valid_phone(phone):
        flash("请输入正确的 11 位手机号", "warning")
        return redirect(url_for("main.sms_list"))

    result = send_test_sms(phone, number, actor=current_user)
    if result["ok"]:
        flash(
            f"测试短信已发送至 {normalize_phone(phone)}（BizId {result['biz_id'] or '-'}）",
            "success",
        )
    else:
        flash(
            f"发送失败：{result['code']} {result['message']}",
            "danger",
        )
    return redirect(url_for("main.sms_list"))

