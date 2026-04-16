"""
项目中介/回款口径：百分比或固定费用 × 中介扣款方向（我方净收 vs 我方另付）。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import Transaction, TransactionType

BROKER_MODE_PERCENT = "percent"
BROKER_MODE_FIXED = "fixed"
BROKER_DIR_WE_PAY = "we_pay_separate"
BROKER_DIR_NET_FROM_BROKER = "net_from_broker"


def referral_ratio_dec(ratio) -> Decimal:
    try:
        r = Decimal(str(ratio if ratio is not None else 0))
    except Exception:
        r = Decimal("0")
    if r < Decimal("0"):
        r = Decimal("0")
    if r > Decimal("1"):
        r = Decimal("1")
    return r


def _project_fee_fields(p: Any) -> tuple[str, str, int]:
    direction = getattr(p, "broker_fee_direction", None) or BROKER_DIR_WE_PAY
    mode = getattr(p, "broker_fee_mode", None) or BROKER_MODE_PERCENT
    fixed = int(getattr(p, "broker_fixed_fee_cents", None) or 0)
    return direction, mode, max(fixed, 0)


def contract_expected_net_and_broker(
    p: Any, expected_total_cents: int
) -> tuple[int, int, int]:
    """
    expected_total_cents = 库中合同基数 + 追加（统一：客户合同全额）。
    返回 (contract_Eg_or_display_gross, expected_net_cents, broker_fee_expected_total_cents)。
    - we_pay_separate: 流水一般按客户全额入账；我方净额 = Eg - 中介（比例/固定）。
    - net_from_broker: 中介从客户款先扣后再付我方；流水一般按净额入账；但合同输入仍为客户全额 Eg。
    """
    Eg = int(expected_total_cents)
    direction, mode, F = _project_fee_fields(p)
    r = referral_ratio_dec(p.referral_ratio)

    if direction == BROKER_DIR_NET_FROM_BROKER:
        if mode == BROKER_MODE_FIXED:
            broker = min(F, Eg)
            En = max(Eg - broker, 0)
            return Eg, En, broker
        broker = int((Decimal(Eg) * r).quantize(Decimal("1")))
        En = int((Decimal(Eg) * (Decimal("1") - r)).quantize(Decimal("1")))
        return Eg, En, broker

    # we_pay_separate：存客户全额
    if mode == BROKER_MODE_FIXED:
        broker = min(F, Eg)
        En = max(Eg - broker, 0)
        return Eg, En, broker
    broker = int((Decimal(Eg) * r).quantize(Decimal("1")))
    En = int((Decimal(Eg) * (Decimal("1") - r)).quantize(Decimal("1")))
    return Eg, En, broker


def received_net_and_broker_estimated(
    p: Any,
    *,
    received_bank_income_cents: int,
    contract_Eg: int,
    contract_En: int,
) -> tuple[int, int]:
    """已回款（流水入账金额）对应的我方净额、估算中介对应量。"""
    R = int(received_bank_income_cents)
    direction, mode, F = _project_fee_fields(p)
    r = referral_ratio_dec(p.referral_ratio)
    Eg = max(int(contract_Eg), 0)
    En = max(int(contract_En), 0)

    if direction == BROKER_DIR_NET_FROM_BROKER:
        # 流水按净额入账：我方净额就是银行入账
        if mode == BROKER_MODE_PERCENT and Decimal("0") < r < Decimal("1"):
            br = int((Decimal(R) * r / (Decimal("1") - r)).quantize(Decimal("1")))
            return R, br
        if mode == BROKER_MODE_FIXED:
            broker_total = min(F, Eg)
            if En <= 0:
                return R, 0
            br = int((Decimal(R) * Decimal(broker_total) / Decimal(En)).quantize(Decimal("1")))
            return R, br
        return R, 0

    if mode == BROKER_MODE_PERCENT:
        rn = int((Decimal(R) * (Decimal("1") - r)).quantize(Decimal("1")))
        br = int((Decimal(R) * r).quantize(Decimal("1")))
        return rn, br
    if Eg <= 0:
        return R, 0
    rn = int((Decimal(R) * Decimal(En) / Decimal(Eg)).quantize(Decimal("1")))
    return rn, R - rn


def build_project_finance(
    p: Any,
    *,
    expected_total_cents: int,
    received_settled_income_bank_cents: int,
) -> dict[str, Any]:
    direction, mode, _F = _project_fee_fields(p)
    Eg, En, broker_contract = contract_expected_net_and_broker(p, expected_total_cents)
    Rn, Br = received_net_and_broker_estimated(
        p,
        received_bank_income_cents=received_settled_income_bank_cents,
        contract_Eg=Eg,
        contract_En=En,
    )
    R = int(received_settled_income_bank_cents)

    if direction == BROKER_DIR_WE_PAY:
        rem_gross = max(Eg - R, 0)
        rem_net = max(En - Rn, 0)
    else:
        gross_eq_received = int(Rn) + int(Br)
        rem_gross = max(Eg - gross_eq_received, 0)
        rem_net = max(En - Rn, 0)

    return {
        "broker_fee_mode": mode,
        "broker_fee_direction": direction,
        "is_net_from_broker": direction == BROKER_DIR_NET_FROM_BROKER,
        "is_percent_mode": mode == BROKER_MODE_PERCENT,
        "contract_display_gross_equiv_cents": Eg,
        "expected_net_cents": En,
        "broker_fee_expected_total_cents": broker_contract,
        "received_bank_income_cents": R,
        "received_net_cents": Rn,
        "broker_estimated_on_received_cents": Br,
        "remaining_gross_cents": rem_gross,
        "remaining_net_cents": rem_net,
    }


def running_settled_cash_balance_by_transaction_id(project_id: int) -> dict[int, int]:
    """已结算流水累计项目账户余额（收入 +、支出 −），按时间正序逐笔入账后余额。"""
    rows = (
        Transaction.query.filter_by(project_id=project_id)
        .filter(Transaction.is_void.is_(False), Transaction.status == "active")
        .order_by(Transaction.occur_date.asc(), Transaction.id.asc())
        .all()
    )
    bal = 0
    after: dict[int, int] = {}
    for t in rows:
        if t.settled:
            if t.type == TransactionType.INCOME.value:
                bal += int(t.amount_cents)
            else:
                bal -= int(t.amount_cents)
        after[int(t.id)] = bal
    return after
