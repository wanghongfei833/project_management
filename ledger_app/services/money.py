"""金额与日期工具：分/元换算、CSV 响应、日期区间解析"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import Response, request

from ..extensions import db
from ..models import Transaction, TransactionType



def yuan_to_cents(y) -> int:
    # Accept Decimal/float/str; store as integer cents.
    return int((Decimal(str(y)) * Decimal("100")).quantize(Decimal("1")))


def cents_to_yuan(cents: int) -> Decimal:
    return (Decimal(int(cents)) / Decimal("100")).quantize(Decimal("0.01"))


def _dividend_cash_balance_cents(project_id: int) -> int:
    """项目当前可用现金（已到账、已生效、未作废）。

    注意要带 ``status == "active"``：待审流水不该参与可分配现金，
    否则一条待审支出会把可分红额度误压小、一条待审收入会放大可用现金。
    """
    def _sum(tx_type: str) -> int:
        return int(
            db.session.query(db.func.coalesce(db.func.sum(Transaction.amount_cents), 0))
            .filter(
                Transaction.project_id == int(project_id),
                Transaction.type == tx_type,
                Transaction.settled.is_(True),
                Transaction.is_void.is_(False),
                Transaction.status == "active",
            )
            .scalar()
            or 0
        )

    return _sum(TransactionType.INCOME.value) - _sum(TransactionType.EXPENSE.value)


def _csv_response(header: list, rows: list, filename: str):
    """带 BOM 的 UTF-8 CSV，避免 Excel 打开中文乱码。"""
    import csv
    import io

    buf = io.StringIO()
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _export_date_range():
    start_d = _parse_iso_date(request.args.get("start"))
    end_d = _parse_iso_date(request.args.get("end"))
    return start_d, end_d


def _parse_iso_date(raw) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _earnings_range_from_request(args) -> tuple[date | None, date | None]:
    """解析收入页的时间筛选参数（按项目实际终止时间 ended_at）。

    - 不带参数：默认本年（1/1 ~ 今天）
    - ?all=1，或 start/end 都留空：不限时间，返回 (None, None)
    """
    today = date.today()
    if args.get("all"):
        return None, None
    if "start" in args or "end" in args:
        return _parse_iso_date(args.get("start")), _parse_iso_date(args.get("end"))
    return date(today.year, 1, 1), today


__all__ = [n for n in dir() if not n.startswith("__")]
