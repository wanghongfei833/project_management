"""项目日志撰写规则 + 「当天没写日志」短信提醒。

规则：某人在某些星期需要为项目写日志（1=周一 … 7=周日，七个都选＝每天）。
每天晚上检查：如果今天该写而没写，就给他发一条短信（暂用待办事项模板）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from .extensions import db
from .models import (
    Project,
    ProjectLogRule,
    ProjectUpdate,
    SmsNotification,
    User,
)
from .sms import (
    allowed_phones,
    is_valid_phone,
    normalize_phone,
    send_sms,
    sms_configured,
)
from .sms_alerts import record_sms

WEEKDAY_LABELS = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
ALL_WEEKDAYS = {1, 2, 3, 4, 5, 6, 7}
TZ_OFFSET = timedelta(hours=8)  # 北京时间


def parse_weekdays(raw) -> set[int]:
    days = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= 7:
            days.add(int(part))
    return days


def weekdays_to_csv(days) -> str:
    return ",".join(str(d) for d in sorted({int(d) for d in days if 1 <= int(d) <= 7}))


def weekdays_label(raw) -> str:
    days = parse_weekdays(raw)
    if not days:
        return "未设置"
    if days == ALL_WEEKDAYS:
        return "每天"
    # 注意：WEEKDAY_LABELS 里已含「周」，这里不能再拼「每周」
    return "每" + "、".join(WEEKDAY_LABELS[d] for d in sorted(days))


# ===== 时间口径：库里存 naive UTC，业务按北京时间 =====


def local_now() -> datetime:
    """北京时间的当前时间（naive）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None) + TZ_OFFSET


def local_day_utc_bounds(local_day: date) -> tuple[datetime, datetime]:
    """把「北京时间的某一天」换算成库里 UTC 的 [起, 止) 区间。"""
    start_utc = datetime.combine(local_day, time.min) - TZ_OFFSET
    return start_utc, start_utc + timedelta(days=1)


def _weekday(d) -> int:
    return int(d.isoweekday())


# ===== 规则与完成情况 =====


def user_has_log(project_id: int, user_id: int, local_day: date) -> bool:
    """该用户当天是否已经为该项目写过日志。"""
    start_utc, end_utc = local_day_utc_bounds(local_day)
    return (
        db.session.query(ProjectUpdate.id)
        .filter(
            ProjectUpdate.project_id == int(project_id),
            ProjectUpdate.created_by_user_id == int(user_id),
            ProjectUpdate.created_at >= start_utc,
            ProjectUpdate.created_at < end_utc,
        )
        .first()
        is not None
    )


def due_rules_for_day(local_day: date) -> list[ProjectLogRule]:
    """当天需要写日志的规则（项目未终止、规则启用）。"""
    weekday = _weekday(local_day)
    rows = (
        db.session.query(ProjectLogRule)
        .join(Project, Project.id == ProjectLogRule.project_id)
        .filter(
            ProjectLogRule.is_active.is_(True),
            Project.status != "ended",
        )
        .order_by(ProjectLogRule.project_id.asc(), ProjectLogRule.user_id.asc())
        .all()
    )
    return [r for r in rows if weekday in parse_weekdays(r.weekdays)]


@dataclass
class LogReminderPlan:
    user_id: int
    username: str
    phone: str
    missing: list[tuple[int, str]] = field(default_factory=list)  # [(project_id, project_name)]

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def projects(self) -> list[tuple[int, str, int]]:
        """[(project_id, project_name, 该项目缺几条)]，按项目聚合。"""
        grouped: dict[tuple[int, str], int] = {}
        for pid, name in self.missing:
            grouped[(pid, name)] = grouped.get((pid, name), 0) + 1
        return [(pid, name, cnt) for (pid, name), cnt in sorted(grouped.items())]


def _already_reminded(user_id: int, local_day: date) -> bool:
    """按「业务日期」判断今天是否已经提醒过（不依赖发送时刻）。"""
    return (
        db.session.query(SmsNotification.id)
        .filter(
            SmsNotification.user_id == int(user_id),
            SmsNotification.trigger == "log_reminder",
            SmsNotification.status == "sent",
            SmsNotification.alert_date == local_day,
        )
        .first()
        is not None
    )


def build_log_reminder_plans(
    *, local_day: date | None = None, force: bool = False
) -> tuple[list[LogReminderPlan], dict[str, int]]:
    """挑出「今天该写日志但没写」的人。"""
    local_day = local_day or local_now().date()
    skipped = {
        "no_phone": 0,
        "invalid_phone": 0,
        "not_allowed": 0,
        "all_done": 0,
        "already_reminded": 0,
    }
    allow = allowed_phones()
    plans: dict[int, LogReminderPlan] = {}

    for rule in due_rules_for_day(local_day):
        user = rule.user
        project = rule.project
        if user is None or project is None:
            continue
        if user_has_log(int(project.id), int(user.id), local_day):
            continue
        plan = plans.setdefault(
            int(user.id),
            LogReminderPlan(
                user_id=int(user.id),
                username=user.username,
                phone=normalize_phone(user.phone),
                missing=[],
            ),
        )
        plan.missing.append((int(project.id), project.name))

    result: list[LogReminderPlan] = []
    for plan in plans.values():
        user = db.session.get(User, plan.user_id)
        if user is None or not user.is_active:
            continue
        if not plan.phone:
            skipped["no_phone"] += 1
            continue
        if not is_valid_phone(plan.phone):
            skipped["invalid_phone"] += 1
            continue
        if allow and plan.phone not in allow:
            skipped["not_allowed"] += 1
            continue
        if not force and _already_reminded(plan.user_id, local_day):
            skipped["already_reminded"] += 1
            continue
        result.append(plan)

    return result, skipped


def send_log_reminders(
    *,
    local_day: date | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    """发送「今天没写日志」提醒。返回汇总信息。"""
    local_day = local_day or local_now().date()
    plans, skipped = build_log_reminder_plans(local_day=local_day, force=force)
    configured = sms_configured()
    summary = {
        "date": local_day.isoformat(),
        "configured": configured,
        "dry_run": bool(dry_run),
        "sent": 0,
        "failed": 0,
        "plans": plans,
        "skipped": skipped,
        "messages": [],
    }

    if dry_run:
        summary["messages"].append(f"dry-run：{local_day} 命中 {len(plans)} 人，未发送")
        return summary
    if not configured:
        summary["messages"].append("短信未配置，本轮跳过")
        return summary

    for plan in plans:
        result = send_sms(plan.phone, plan.missing_count)
        record_sms(
            user_id=plan.user_id,
            phone=plan.phone,
            username=plan.username,
            pending_count=plan.missing_count,
            result=result,
            trigger="log_reminder",
            projects=plan.projects,
            alert_date=local_day,
        )
        db.session.commit()
        if result.get("ok"):
            summary["sent"] += 1
            summary["messages"].append(
                f"{plan.username}（{plan.phone}）已提醒：{plan.missing_count} 条日志未写"
            )
        else:
            summary["failed"] += 1
            summary["messages"].append(
                f"{plan.username}（{plan.phone}）发送失败：{result.get('code')} {result.get('message')}"
            )
    return summary


def rules_for_project(project_id: int) -> list[ProjectLogRule]:
    return (
        ProjectLogRule.query.filter_by(project_id=int(project_id), is_active=True)
        .order_by(ProjectLogRule.id.asc())
        .all()
    )
