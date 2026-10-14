"""时间口径工具：数据库统一存 naive UTC，业务判断按北京时间。

单独成模块是为了避免 log_rules / sms_alerts / sms_delivery 之间循环导入。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

TZ_OFFSET = timedelta(hours=8)  # 北京时间


def local_now() -> datetime:
    """北京时间的当前时间（naive）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None) + TZ_OFFSET


def to_local(utc_naive: datetime | None) -> datetime | None:
    """库里的 naive UTC → 北京时间（naive）。"""
    return None if utc_naive is None else utc_naive + TZ_OFFSET


def from_local(local_naive: datetime) -> datetime:
    """北京时间（naive）→ 库里存的 naive UTC。"""
    return local_naive - TZ_OFFSET


def local_day_utc_bounds(local_day: date) -> tuple[datetime, datetime]:
    """把「北京时间的某一天」换算成库里 UTC 的 [起, 止) 区间。"""
    start_utc = datetime.combine(local_day, time.min) - TZ_OFFSET
    return start_utc, start_utc + timedelta(days=1)
