"""主合同项目与分包项目之间的动态合同承诺和内部结算。"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_

from ..extensions import db
from ..models import (
    Project,
    ProjectSubcontractSettlement,
    Transaction,
)

ACTIVE_SETTLEMENT_STATUSES = ("pending_payment", "pending_receipt", "closed")


@dataclass(frozen=True)
class ChildContractSummary:
    child: Project
    contract_cents: int
    paid_cents: int
    pending_payment_cents: int
    unpaid_commitment_cents: int
    overpaid_cents: int
    available_to_pay_cents: int


@dataclass(frozen=True)
class ParentContractSummary:
    children: list[ChildContractSummary]
    contract_cents: int
    paid_cents: int
    pending_payment_cents: int
    overpaid_cents: int
    available_to_pay_cents: int
    gross_margin_cents: int


@dataclass(frozen=True)
class ChildReceivableSummary:
    parent_project: Project | None
    contract_cents: int
    received_cents: int
    pending_receipt_cents: int
    outstanding_cents: int


def _settlement_amount(settlement: ProjectSubcontractSettlement) -> int:
    return int(settlement.amount_cents or 0)


def parent_contract_summary(parent_project_id: int) -> ParentContractSummary:
    parent = db.session.get(Project, int(parent_project_id))
    if parent is None:
        return ParentContractSummary([], 0, 0, 0, 0, 0, 0)

    children = (
        Project.query.filter(Project.parent_project_id == int(parent.id))
        .order_by(Project.name.asc())
        .all()
    )
    settlements = ProjectSubcontractSettlement.query.filter(
        ProjectSubcontractSettlement.parent_project_id == int(parent.id),
        ProjectSubcontractSettlement.status.in_(ACTIVE_SETTLEMENT_STATUSES),
    ).all()

    by_child: dict[int, list[ProjectSubcontractSettlement]] = {}
    for settlement in settlements:
        by_child.setdefault(int(settlement.child_project_id), []).append(settlement)

    rows: list[ChildContractSummary] = []
    total_contract = 0
    total_paid = 0
    total_pending = 0
    total_overpaid = 0
    for child in children:
        contract_cents = int(child.expected_income_cents or 0)
        child_settlements = by_child.get(int(child.id), [])
        paid_cents = sum(
            _settlement_amount(s)
            for s in child_settlements
            if s.status in ("pending_receipt", "closed")
        )
        pending_payment_cents = sum(
            _settlement_amount(s)
            for s in child_settlements
            if s.status == "pending_payment"
        )
        unpaid = max(contract_cents - paid_cents, 0)
        overpaid = max(paid_cents - contract_cents, 0)
        available = max(
            contract_cents - paid_cents - pending_payment_cents, 0
        )
        rows.append(
            ChildContractSummary(
                child=child,
                contract_cents=contract_cents,
                paid_cents=paid_cents,
                pending_payment_cents=pending_payment_cents,
                unpaid_commitment_cents=unpaid,
                overpaid_cents=overpaid,
                available_to_pay_cents=available,
            )
        )
        total_contract += contract_cents
        total_paid += paid_cents
        total_pending += pending_payment_cents
        total_overpaid += overpaid

    own_contract = int(parent.expected_income_cents or 0)
    return ParentContractSummary(
        children=rows,
        contract_cents=total_contract,
        paid_cents=total_paid,
        pending_payment_cents=total_pending,
        overpaid_cents=total_overpaid,
        available_to_pay_cents=max(total_contract - total_paid - total_pending, 0),
        gross_margin_cents=own_contract - total_contract,
    )


def child_receivable_summary(child_project_id: int) -> ChildReceivableSummary:
    child = db.session.get(Project, int(child_project_id))
    if child is None or not child.parent_project_id:
        return ChildReceivableSummary(None, 0, 0, 0, 0)

    parent = db.session.get(Project, int(child.parent_project_id))
    contract_cents = int(child.expected_income_cents or 0)
    settlements = ProjectSubcontractSettlement.query.filter(
        ProjectSubcontractSettlement.child_project_id == int(child.id),
        ProjectSubcontractSettlement.status.in_(ACTIVE_SETTLEMENT_STATUSES),
    ).all()
    received_cents = sum(
        _settlement_amount(s) for s in settlements if s.status == "closed"
    )
    pending_receipt_cents = sum(
        _settlement_amount(s)
        for s in settlements
        if s.status == "pending_receipt"
    )
    return ChildReceivableSummary(
        parent_project=parent,
        contract_cents=contract_cents,
        received_cents=received_cents,
        pending_receipt_cents=pending_receipt_cents,
        outstanding_cents=max(contract_cents - received_cents, 0),
    )


def settle_subcontract_amount(
    settlement: ProjectSubcontractSettlement, paid_at
) -> Transaction:
    """主项目付款后，在分包项目侧生成待确认收入。"""
    child = db.session.get(Project, int(settlement.child_project_id))
    parent = db.session.get(Project, int(settlement.parent_project_id))
    if child is None or parent is None:
        raise ValueError("内部结算项目不存在")
    tx = Transaction(
        project_id=int(child.id),
        status="active",
        type="income",
        amount_cents=int(settlement.amount_cents),
        occur_date=settlement.parent_transaction.occur_date,
        settled=False,
        fund_account="public",
        counterparty=f"分包款[{parent.name}]",
        note=(
            f"[SUBCONTRACT] 来自主合同项目「{parent.name}」的分包款"
            + (f"；{settlement.note}" if settlement.note else "")
        ),
        created_by_user_id=settlement.created_by_user_id,
    )
    db.session.add(tx)
    db.session.flush()
    settlement.child_transaction_id = int(tx.id)
    settlement.status = "pending_receipt"
    settlement.paid_at = paid_at
    return tx


def mark_subcontract_received(settlement: ProjectSubcontractSettlement, received_at) -> None:
    settlement.status = "closed"
    settlement.received_at = received_at


def settlement_for_transaction(
    transaction_id: int,
) -> ProjectSubcontractSettlement | None:
    return ProjectSubcontractSettlement.query.filter(
        or_(
            ProjectSubcontractSettlement.parent_transaction_id == int(transaction_id),
            ProjectSubcontractSettlement.child_transaction_id == int(transaction_id),
        )
    ).first()


__all__ = [name for name in dir() if not name.startswith("__")]
