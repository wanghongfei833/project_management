"""待办短信提醒。

规则：**每晚 20:00 检查一次**——只要某人名下还有没处理的申请，就给他发一条短信；
一天最多一条（按「业务日期」去重，手动发送也算当天已发）。

短信按人聚合：一次给一个人发一条，正文里的 ``${n1}`` 是该人当前待处理条数、
``${n2}`` 是最久那条已积压的小时数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from datetime import datetime, timedelta, timezone

from .extensions import db
from .models import Role, SmsNotification, SmsNotificationProject, User
from .pending import PendingItem, pending_items_for_user
from .timeutil import TZ_OFFSET
from .sms import (
    is_valid_phone,
    normalize_phone,
    send_sms,
    sms_config,
    sms_configured,
    allowed_phones,
    mask_phone,
)

# 每晚这个时间（北京时间）发一次；由 pm-sms-alert.timer 定时触发
ALERT_HOUR = 20


def _utcnow() -> datetime:
    """与库里的 created_at 口径一致：naive UTC。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class AlertPlan:
    user_id: int
    username: str
    phone: str
    pending_count: int
    oldest_at: datetime | None
    projects: list[tuple[int | None, str, int]] = field(default_factory=list)


def _project_breakdown(items: list[PendingItem]) -> list[tuple[int | None, str, int]]:
    """[(project_id, project_name, item_count)]，按项目聚合待办条数。"""
    grouped: dict[tuple[int | None, str], int] = {}
    for item in items:
        key = (item.project_id, item.project_name)
        grouped[key] = grouped.get(key, 0) + 1
    return sorted(
        ((pid, name, count) for (pid, name), count in grouped.items()),
        key=lambda row: (-row[2], row[1] or ""),
    )


def _already_alerted(user_id: int, local_day: date) -> bool:
    """当天是否已经提醒过（按业务日期判断，重跑不会重复发）。"""
    return (
        db.session.query(SmsNotification.id)
        .filter(
            SmsNotification.user_id == int(user_id),
            SmsNotification.status == "sent",
            SmsNotification.trigger.in_(["auto", "manual"]),
            SmsNotification.alert_date == local_day,
        )
        .first()
        is not None
    )


def build_alert_plans(
    *,
    now: datetime | None = None,
    user_id: int | None = None,
    force: bool = False,
) -> tuple[list[AlertPlan], dict[str, int]]:
    """挑出需要发短信的人（有未处理事项、且当天还没提醒过）。"""
    local_day = (now or _utcnow()) + TZ_OFFSET
    local_day = local_day.date()
    plans: list[AlertPlan] = []
    skipped = {
        "no_phone": 0,
        "invalid_phone": 0,
        "not_allowed": 0,
        "no_pending": 0,
        "already_reminded": 0,
    }
    allow = allowed_phones()

    query = User.query.filter(User.is_active.is_(True))
    if user_id:
        query = query.filter(User.id == int(user_id))

    for user in query.order_by(User.id.asc()).all():
        phone = normalize_phone(user.phone)
        if not phone:
            skipped["no_phone"] += 1
            continue
        if not is_valid_phone(phone):
            skipped["invalid_phone"] += 1
            continue
        if allow and phone not in allow:
            skipped["not_allowed"] += 1
            continue

        items = pending_items_for_user(
            int(user.id), is_admin_user=(user.role == Role.ADMIN.value)
        )
        todo = [i for i in items if not i.approved_by_me]
        if not todo:
            skipped["no_pending"] += 1
            continue

        if not force and _already_alerted(int(user.id), local_day):
            skipped["already_reminded"] += 1
            continue

        oldest = min((i.created_at for i in todo if i.created_at is not None), default=None)
        plans.append(
            AlertPlan(
                user_id=int(user.id),
                username=user.username,
                phone=phone,
                pending_count=len(todo),
                oldest_at=oldest,
                projects=_project_breakdown(todo),
            )
        )

    return plans, skipped


def record_sms(
    *,
    user_id: int | None,
    phone: str,
    username: str | None,
    pending_count: int,
    result: dict,
    trigger: str,
    projects: list[tuple[int | None, str, int]],
    alert_date: date | None = None,
) -> SmsNotification:
    """写一条短信记录（供待办提醒与日志提醒共用）。"""
    ok = bool(result.get("ok"))
    row = SmsNotification(
        user_id=user_id,
        username=username,
        phone=phone,
        pending_count=int(pending_count),
        content=result.get("content"),
        status="sent" if ok else "failed",
        provider="aliyun",
        template_code=result.get("template_code"),
        request_id=(result.get("biz_id") or result.get("request_id") or None),
        error=None if ok else f"{result.get('code') or ''}: {result.get('message') or ''}".strip(": "),
        trigger=trigger,
        alert_date=alert_date,
    )
    for pid, pname, count in projects:
        row.projects.append(
            SmsNotificationProject(
                project_id=pid, project_name=pname, item_count=int(count)
            )
        )
    db.session.add(row)
    return row


def send_pending_alerts(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    force: bool = False,
    user_id: int | None = None,
    trigger: str = "auto",
) -> dict:
    """执行一轮提醒。返回 {"configured", "sent", "failed", "skipped", "plans", "messages"}。"""
    plans, skipped = build_alert_plans(now=now, user_id=user_id, force=force)
    configured = sms_configured()
    summary = {
        "configured": configured,
        "dry_run": bool(dry_run),
        "sent": 0,
        "failed": 0,
        "plans": plans,
        "skipped": skipped,
        "messages": [],
    }

    if dry_run:
        summary["messages"].append(
            f"dry-run：命中 {len(plans)} 人，未实际发送、未写记录"
        )
        return summary

    if not configured:
        summary["messages"].append(
            "短信未配置（ALIYUN_SMS_ACCESS_KEY_ID / ACCESS_KEY_SECRET / SIGN_NAME），本轮跳过"
        )
        return summary

    for plan in plans:
        # n2 = 最久那条待办已积压的小时数（至少 1 小时）
        hours = 1
        if plan.oldest_at is not None:
            hours = max(int((_utcnow() - plan.oldest_at).total_seconds() // 3600), 1)
        result = send_sms(plan.phone, plan.pending_count, hours=hours)
        # 记下「业务日期」，同一天不会重复发（自动/手动都算）
        local_day = ((now or _utcnow()) + TZ_OFFSET).date()
        record_sms(
            user_id=plan.user_id,
            phone=plan.phone,
            username=plan.username,
            pending_count=plan.pending_count,
            result=result,
            trigger=trigger,
            projects=plan.projects,
            alert_date=local_day,
        )
        db.session.commit()
        if result.get("ok"):
            summary["sent"] += 1
            summary["messages"].append(
                f"{plan.username}（{plan.phone}）已发送，{plan.pending_count} 条待办"
            )
        else:
            summary["failed"] += 1
            summary["messages"].append(
                f"{plan.username}（{plan.phone}）发送失败："
                f"{result.get('code')} {result.get('message')}"
            )

    return summary


def send_test_sms(
    phone: str, number: int = 1, *, hours: int = 1, actor: User | None = None
) -> dict:
    """给指定号码发一条测试短信，并记一条 trigger=test 的记录。"""
    target = normalize_phone(phone)
    result = send_sms(target, number, hours=hours)
    if result.get("configured"):
        record_sms(
            user_id=None,
            phone=target,
            username=(actor.username if actor else None),
            pending_count=int(number),
            result=result,
            trigger="test",
            projects=[],
        )
        db.session.commit()
    return result


def sms_status() -> dict:
    """给管理页展示的配置状态（不暴露密钥，只显示是否配置）。"""
    cfg = sms_config()
    allow = allowed_phones()
    return {
        "configured": sms_configured(cfg),
        "sign_name": cfg["sign_name"],
        "template_code": cfg["template_code"],
        "region_id": cfg["region_id"],
        "has_access_key_id": bool(cfg["access_key_id"]),
        "has_access_key_secret": bool(cfg["access_key_secret"]),
        "alert_hour": ALERT_HOUR,
        "once_per_day": True,
        "test_mode": bool(allow),
        "allow_phones": [mask_phone(p) for p in sorted(allow)],
    }
