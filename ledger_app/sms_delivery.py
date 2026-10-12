"""短信投递回执同步。

SendSms 的返回只代表「阿里云受理」，不等于「用户收到」——号码空号、停机、
号段不支持时，受理成功但投递失败（错误码如 PORT_NOT_REGISTERED）。
这里用 QuerySendDetails 事后同步真实投递状态，写到 sms_notifications 上。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .extensions import db
from .models import SmsNotification
from .sms import query_send_details
from .log_rules import TZ_OFFSET


def _parse_send_date(raw) -> datetime | None:
    """阿里云回执里的 'YYYY-MM-DD HH:MM:SS' 是北京时间 → 转成 naive UTC 便于比对。"""
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S") - TZ_OFFSET
    except ValueError:
        return None


def sync_delivery_status(*, days: int = 2, tolerance_minutes: int = 10) -> dict:
    """同步最近几天的投递回执。返回汇总（已更新/仍未回执/失败数）。"""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    since = now_utc - timedelta(days=max(int(days), 1))
    rows = (
        SmsNotification.query.filter(
            SmsNotification.created_at >= since,
            SmsNotification.status == "sent",
        )
        .order_by(SmsNotification.id.asc())
        .all()
    )
    summary = {"checked": 0, "updated": 0, "delivered": 0, "failed": 0, "pending": 0, "errors": []}
    if not rows:
        return summary

    # 按 (手机号, 北京日期) 归组，一天一个号码只查一次
    buckets: dict[tuple[str, str], list[SmsNotification]] = {}
    for r in rows:
        if r.delivery_status == "成功":
            continue
        local_day = (r.created_at + TZ_OFFSET).strftime("%Y%m%d")
        buckets.setdefault((r.phone, local_day), []).append(r)

    for (phone, day), records in buckets.items():
        res = query_send_details(phone, day)
        summary["checked"] += 1
        if not res.get("ok"):
            summary["errors"].append(f"{phone} {day}: {res.get('code')} {res.get('message')}")
            continue
        api_rows = []
        for r in res["records"]:
            ts = _parse_send_date(r.get("send_date"))
            if ts is not None:
                api_rows.append((ts, r))
        for rec in records:
            best = None
            for ts, api in api_rows:
                delta = abs((ts - rec.created_at).total_seconds())
                if delta <= tolerance_minutes * 60 and (best is None or delta < best[0]):
                    best = (delta, api)
            if best is None:
                summary["pending"] += 1
                continue
            api = best[1]
            rec.delivery_status = api["status_text"]
            rec.delivery_error = api["err_code"] or None
            rec.delivered_at = _parse_send_date(api.get("receive_date"))
            summary["updated"] += 1
            if api["send_status"] == 3:
                summary["delivered"] += 1
            elif api["send_status"] == 2:
                summary["failed"] += 1
            else:
                summary["pending"] += 1
    db.session.commit()
    return summary
