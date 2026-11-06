"""项目资金账户：对公账户、资金池与内部划转。"""
from __future__ import annotations

from dataclasses import dataclass

from datetime import datetime, timezone

from sqlalchemy import text

from ..extensions import db
from ..models import (
    FundAccount,
    FundTransfer,
    Project,
    ProjectFundPool,
    Transaction,
    TransactionType,
)

FUND_ACCOUNT_LABELS = {
    FundAccount.PUBLIC.value: "对公账户",
    FundAccount.POOL.value: "项目资金池",
}

FUND_TRANSFER_METHODS = ["银行转账", "工资代发", "现金", "个人转账", "其他"]


@dataclass(frozen=True)
class FundBalances:
    public_cents: int
    pool_cents: int

    @property
    def total_cents(self) -> int:
        return int(self.public_cents) + int(self.pool_cents)


def fund_account_label(value: str | None) -> str:
    return FUND_ACCOUNT_LABELS.get(
        value or FundAccount.PUBLIC.value, FundAccount.PUBLIC.value
    )


def ensure_project_fund_pool(
    project: Project, *, commit: bool = False
) -> ProjectFundPool:
    """取项目资金池；不存在时以项目负责人为保管人创建一个。"""
    pool = ProjectFundPool.query.filter_by(project_id=int(project.id)).first()
    if pool is not None:
        return pool

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.execute(
        text(
            """
            INSERT OR IGNORE INTO project_fund_pools
              (project_id, name, custodian_user_id, created_at, updated_at)
            VALUES
              (:project_id, :name, :custodian_user_id, :created_at, :updated_at)
            """
        ),
        {
            "project_id": int(project.id),
            "name": "项目资金池",
            "custodian_user_id": project.leader_user_id,
            "created_at": now,
            "updated_at": now,
        },
    )
    pool = ProjectFundPool.query.filter_by(project_id=int(project.id)).first()
    if pool is None:
        raise RuntimeError("项目资金池创建失败")
    if commit:
        db.session.commit()
    return pool


def project_fund_balances(project_ids: list[int] | set[int] | tuple[int, ...]) -> FundBalances:
    """按已生效、已结算流水和未作废划转计算账户余额。"""
    ids = {int(pid) for pid in project_ids if pid}
    if not ids:
        return FundBalances(0, 0)

    public_cents = 0
    pool_cents = 0
    rows = (
        Transaction.query.filter(
            Transaction.project_id.in_(ids),
            Transaction.status == "active",
            Transaction.is_void.is_(False),
            Transaction.settled.is_(True),
        )
        .order_by(Transaction.occur_date.asc(), Transaction.id.asc())
        .all()
    )
    for tx in rows:
        account = tx.fund_account or FundAccount.PUBLIC.value
        if tx.type == TransactionType.INCOME.value:
            delta = int(tx.amount_cents or 0)
        else:
            delta = -int(
                tx.paid_amount_cents
                if tx.paid_amount_cents is not None
                else tx.amount_cents or 0
            )
        if account == FundAccount.POOL.value:
            pool_cents += delta
        else:
            public_cents += delta

    transfers = FundTransfer.query.filter(
        FundTransfer.project_id.in_(ids),
        FundTransfer.is_void.is_(False),
    ).all()
    for transfer in transfers:
        amount = int(transfer.amount_cents or 0)
        if (
            transfer.from_account == FundAccount.PUBLIC.value
            and transfer.to_account == FundAccount.POOL.value
        ):
            public_cents -= amount
            pool_cents += amount
        elif (
            transfer.from_account == FundAccount.POOL.value
            and transfer.to_account == FundAccount.PUBLIC.value
        ):
            pool_cents -= amount
            public_cents += amount

    return FundBalances(public_cents=public_cents, pool_cents=pool_cents)


def transfer_accounts(direction: str) -> tuple[str, str]:
    if direction == "from_pool":
        return FundAccount.POOL.value, FundAccount.PUBLIC.value
    return FundAccount.PUBLIC.value, FundAccount.POOL.value


def account_balance(balances: FundBalances, account: str) -> int:
    if account == FundAccount.POOL.value:
        return int(balances.pool_cents)
    return int(balances.public_cents)


__all__ = [name for name in dir() if not name.startswith("__")]
