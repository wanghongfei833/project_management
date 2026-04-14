from decimal import Decimal
from datetime import date
from pathlib import Path

from flask import current_app

from .extensions import db
from .models import (
    Role,
    Shareholder,
    ShareholdingPeriod,
    Transaction,
    TransactionEditApproval,
    TransactionEditRequest,
    User,
    UserShareholderLink,
)


def _purge_transactions_without_project() -> int:
    """删除未绑定项目的流水及其凭证、修改审批数据（策略：流水必须归属项目）。"""
    orphans = Transaction.query.filter(Transaction.project_id.is_(None)).all()
    if not orphans:
        return 0

    upload_root = Path(current_app.config.get("UPLOAD_FOLDER", "uploads"))
    orphan_ids = [tx.id for tx in orphans]

    edit_req_ids = [
        int(r[0])
        for r in db.session.query(TransactionEditRequest.id)
        .filter(TransactionEditRequest.transaction_id.in_(orphan_ids))
        .all()
    ]
    if edit_req_ids:
        db.session.query(TransactionEditApproval).filter(
            TransactionEditApproval.request_id.in_(edit_req_ids)
        ).delete(synchronize_session=False)
        db.session.query(TransactionEditRequest).filter(
            TransactionEditRequest.id.in_(edit_req_ids)
        ).delete(synchronize_session=False)

    for tx in orphans:
        for att in list(tx.attachments):
            try:
                (upload_root / att.stored_path).unlink(missing_ok=True)
            except Exception:
                pass
            db.session.delete(att)
        db.session.delete(tx)

    return len(orphans)


def ensure_seed_data():
    _purge_transactions_without_project()

    # Initial admin account per user request:
    # username: admin
    # password: admin123!
    admin = User.query.filter_by(username="admin").first()
    if not admin:
        admin = User(username="admin", role=Role.ADMIN.value)
        admin.set_password("admin123!")
        db.session.add(admin)
        db.session.flush()

    # Create a matching shareholder for admin if absent.
    sh = Shareholder.query.filter_by(display_name="管理员").first()
    if not sh:
        sh = Shareholder(display_name="管理员", note="系统初始化创建")
        db.session.add(sh)
        db.session.flush()

    link = UserShareholderLink.query.filter_by(user_id=admin.id).first()
    if not link:
        db.session.add(UserShareholderLink(user_id=admin.id, shareholder_id=sh.id))

    # Default shareholding period is left for the admin to configure,
    # but create a placeholder 100% period to keep the system usable.
    period = ShareholdingPeriod.query.filter_by(shareholder_id=sh.id).first()
    if not period:
        db.session.add(
            ShareholdingPeriod(
                shareholder_id=sh.id,
                start_date=date(2000, 1, 1),
                end_date=None,
                ratio=Decimal("1.0"),
            )
        )

    db.session.commit()

