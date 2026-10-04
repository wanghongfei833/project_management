"""待办超时短信提醒。

规则：某个申请在他人那里积压超过 6 小时，就给「还没同意的人」发短信；
只要还没处理完，之后每 6 小时再发一条（靠发送记录做冷却，不会重复轰炸）。

短信按人聚合：一次给一个人发一条，正文里的 ``${number}`` 是该人当前待处理条数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .extensions import db
from .models import Role, SmsNotification, SmsNotificationProject, User
from .pending import PendingItem, pending_items_for_user
from .sms import (
    is_valid_phone,
    normalize_phone,
    send_sms,
    sms_config,
    sms_configured,
)

ALERT_AFTER = timedelta(hours=6)
RESEND_EVERY = timedelta(hours=6)


def _utcnow() -> datetime:
    """与库里的 created_at 口径一致：naive UTC。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class AlertPlan:
    user_id: int
    username: str
    phone: str
    pending_count: int
    overdue_count: int
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


def _last_sent_at(user_id: int) -> datetime | None:
    row = (
        db.session.query(SmsNotification.created_at)
        .filter(
            SmsNotification.user_id == user_id,
            SmsNotification.status == "sent",
            SmsNotification.trigger.in_(["auto", "manual"]),
        )
        .order_by(SmsNotification.created_at.desc(), SmsNotification.id.desc())
        .first()
    )
    return row[0] if row else None


def build_alert_plans(
    *,
    now: datetime | None = None,
    user_id: int | None = None,
    force: bool = False,
) -> tuple[list[AlertPlan], dict[str, int]]:
    """挑出需要发短信的人。返回 (plans, 跳过原因统计)。"""
    now = now or _utcnow()
    plans: list[AlertPlan] = []
    skipped = {
        "no_phone": 0,
        "invalid_phone": 0,
        "no_pending": 0,
        "not_overdue": 0,
        "cooling": 0,
    }

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

        items = pending_items_for_user(
            int(user.id), is_admin_user=(user.role == Role.ADMIN.value)
        )
        todo = [i for i in items if not i.approved_by_me]
        if not todo:
            skipped["no_pending"] += 1
            continue

        overdue = [
            i
            for i in todo
            if i.created_at is not None and (now - i.created_at) >= ALERT_AFTER
        ]
        if not overdue:
            skipped["not_overdue"] += 1
            continue

        if not force:
            last = _last_sent_at(int(user.id))
            if last is not None and (now - last) < RESEND_EVERY:
                skipped["cooling"] += 1
                continue

        oldest = min(
            (i.created_at for i in overdue if i.created_at is not None), default=None
        )
        plans.append(
            AlertPlan(
                user_id=int(user.id),
                username=user.username,
                phone=phone,
                pending_count=len(todo),
                overdue_count=len(overdue),
                oldest_at=oldest,
                projects=_project_breakdown(todo),
            )
        )

    return plans, skipped


def _record(
    *,
    plan: AlertPlan | None,
    phone: str,
    username: str | None,
    pending_count: int,
    result: dict,
    trigger: str,
    projects: list[tuple[int | None, str, int]],
) -> SmsNotification:
    ok = bool(result.get("ok"))
    row = SmsNotification(
        user_id=plan.user_id if plan else None,
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
        result = send_sms(plan.phone, plan.pending_count)
        _record(
            plan=plan,
            phone=plan.phone,
            username=plan.username,
            pending_count=plan.pending_count,
            result=result,
            trigger=trigger,
            projects=plan.projects,
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


def send_test_sms(phone: str, number: int = 1, *, actor: User | None = None) -> dict:
    """给指定号码发一条测试短信，并记一条 trigger=test 的记录。"""
    target = normalize_phone(phone)
    result = send_sms(target, number)
    if result.get("configured"):
        _record(
            plan=None,
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
    return {
        "configured": sms_configured(cfg),
        "sign_name": cfg["sign_name"],
        "template_code": cfg["template_code"],
        "region_id": cfg["region_id"],
        "has_access_key_id": bool(cfg["access_key_id"]),
        "has_access_key_secret": bool(cfg["access_key_secret"]),
        "alert_after_hours": ALERT_AFTER.total_seconds() / 3600,
        "resend_every_hours": RESEND_EVERY.total_seconds() / 3600,
    }
