from __future__ import annotations

from datetime import datetime, date
from enum import Enum

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


class Role(str, Enum):
    ADMIN = "admin"
    VIEWER = "viewer"


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default=Role.VIEWER.value)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    shareholder_link = db.relationship(
        "UserShareholderLink", back_populates="user", uselist=False
    )

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id: str):
    try:
        uid = int(user_id)
    except ValueError:
        return None
    return db.session.get(User, uid)


class Shareholder(db.Model):
    __tablename__ = "shareholders"

    id = db.Column(db.Integer, primary_key=True)
    display_name = db.Column(db.String(128), unique=True, nullable=False)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    periods = db.relationship("ShareholdingPeriod", back_populates="shareholder")
    user_link = db.relationship(
        "UserShareholderLink", back_populates="shareholder", uselist=False
    )


class UserShareholderLink(db.Model):
    __tablename__ = "user_shareholder_links"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    shareholder_id = db.Column(
        db.Integer, db.ForeignKey("shareholders.id"), primary_key=True
    )
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", back_populates="shareholder_link")
    shareholder = db.relationship("Shareholder", back_populates="user_link")


class ShareholdingPeriod(db.Model):
    __tablename__ = "shareholding_periods"

    id = db.Column(db.Integer, primary_key=True)
    shareholder_id = db.Column(
        db.Integer, db.ForeignKey("shareholders.id"), nullable=False, index=True
    )
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=True)  # null means ongoing
    ratio = db.Column(db.Numeric(10, 6), nullable=False)  # 0~1

    shareholder = db.relationship("Shareholder", back_populates="periods")


class ProjectStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), unique=True, nullable=False)
    expected_income_cents = db.Column(db.BigInteger, nullable=False, default=0)
    referral_ratio = db.Column(db.Numeric(10, 6), nullable=False, default=0)  # 0~1
    # 中介：percent=按比例；fixed=固定总费用（与 broker_fixed_fee_cents 配合）
    broker_fee_mode = db.Column(db.String(16), nullable=False, default="percent")
    # net_from_broker=中介从客户款扣后再付我方（流水为净额）；we_pay_separate=我方另付/分成（流水为全额）
    broker_fee_direction = db.Column(db.String(32), nullable=False, default="we_pay_separate")
    broker_fixed_fee_cents = db.Column(db.BigInteger, nullable=False, default=0)
    status = db.Column(db.String(16), nullable=False, default=ProjectStatus.OPEN.value)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    leader_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    planned_start_date = db.Column(db.Date, nullable=True)
    planned_end_date = db.Column(db.Date, nullable=True)

    transactions = db.relationship("Transaction", back_populates="project")
    adjustments = db.relationship("ProjectExpectedIncomeAdjustment", back_populates="project")
    members = db.relationship("ProjectMember", back_populates="project")
    leader = db.relationship("User", foreign_keys=[leader_user_id])
    updates = db.relationship("ProjectUpdate", back_populates="project")
    dividend_distributions = db.relationship(
        "ProjectDividendDistribution", back_populates="project"
    )
    activity_logs = db.relationship("ProjectActivityLog", back_populates="project")


class ProjectActivityLog(db.Model):
    """项目维度的操作审计日志（与「进展记录」分离，偏系统事件）。"""

    __tablename__ = "project_activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action = db.Column(db.String(64), nullable=False, index=True)
    summary = db.Column(db.String(512), nullable=False)
    detail = db.Column(db.Text, nullable=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    project = db.relationship("Project", back_populates="activity_logs")
    actor = db.relationship("User", foreign_keys=[actor_user_id])


class ProjectDividendDistribution(db.Model):
    """分红划出记录（会对应一笔支出流水）。"""

    __tablename__ = "project_dividend_distributions"

    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(
        db.Integer, db.ForeignKey("transactions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recipient_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    recipient_name = db.Column(db.String(128), nullable=True)
    amount_cents = db.Column(db.BigInteger, nullable=False)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    project = db.relationship("Project", back_populates="dividend_distributions")
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    recipient_user = db.relationship("User", foreign_keys=[recipient_user_id])
    transaction = db.relationship("Transaction")


class ProjectDividendRecipient(db.Model):
    """某项目可分红对象（名字），用于下拉复用。"""

    __tablename__ = "project_dividend_recipients"

    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    name = db.Column(db.String(128), primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    project = db.relationship("Project")
    created_by = db.relationship("User")


class ProjectExpectedIncomeAdjustment(db.Model):
    __tablename__ = "project_expected_income_adjustments"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True
    )
    amount_cents = db.Column(db.BigInteger, nullable=False)  # positive to add expected income
    note = db.Column(db.Text, nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    project = db.relationship("Project", back_populates="adjustments")


class ProjectMember(db.Model):
    __tablename__ = "project_members"

    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id"), primary_key=True, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    project = db.relationship("Project", back_populates="members")
    user = db.relationship("User")


class ProjectDeleteRequest(db.Model):
    __tablename__ = "project_delete_requests"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True
    )
    status = db.Column(db.String(16), nullable=False, default="open")  # open/executed/cancelled
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    executed_at = db.Column(db.DateTime, nullable=True)
    executed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    approvals = db.relationship("ProjectDeleteApproval", back_populates="request")


class ProjectDeleteApproval(db.Model):
    __tablename__ = "project_delete_approvals"

    request_id = db.Column(
        db.Integer, db.ForeignKey("project_delete_requests.id"), primary_key=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    approved_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    request = db.relationship("ProjectDeleteRequest", back_populates="approvals")
    user = db.relationship("User")


class ProjectEndDateChangeRequest(db.Model):
    __tablename__ = "project_end_date_change_requests"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status = db.Column(db.String(16), nullable=False, default="open")  # open/executed/cancelled

    old_end_date = db.Column(db.Date, nullable=False)
    new_end_date = db.Column(db.Date, nullable=False)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    executed_at = db.Column(db.DateTime, nullable=True)
    executed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    project = db.relationship("Project")
    approvals = db.relationship("ProjectEndDateChangeApproval", back_populates="request")


class ProjectEndDateChangeApproval(db.Model):
    __tablename__ = "project_end_date_change_approvals"

    request_id = db.Column(
        db.Integer,
        db.ForeignKey("project_end_date_change_requests.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    approved_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    request = db.relationship("ProjectEndDateChangeRequest", back_populates="approvals")
    user = db.relationship("User")


class ProjectUpdate(db.Model):
    __tablename__ = "project_updates"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    body = db.Column(db.Text, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    project = db.relationship("Project", back_populates="updates")
    attachments = db.relationship("ProjectUpdateAttachment", back_populates="update")


class ProjectUpdateAttachment(db.Model):
    __tablename__ = "project_update_attachments"

    id = db.Column(db.Integer, primary_key=True)
    update_id = db.Column(
        db.Integer, db.ForeignKey("project_updates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename = db.Column(db.String(256), nullable=False)
    stored_path = db.Column(db.String(512), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    uploaded_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    update = db.relationship("ProjectUpdate", back_populates="attachments")


class TransactionType(str, Enum):
    INCOME = "income"
    EXPENSE = "expense"


class Transaction(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=True)
    type = db.Column(db.String(16), nullable=False, index=True)
    amount_cents = db.Column(db.BigInteger, nullable=False)
    occur_date = db.Column(db.Date, nullable=False, default=date.today)
    settled = db.Column(db.Boolean, nullable=False, default=True)
    counterparty = db.Column(db.String(256), nullable=True)
    note = db.Column(db.Text, nullable=True)
    is_void = db.Column(db.Boolean, nullable=False, default=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    project = db.relationship("Project", back_populates="transactions")
    attachments = db.relationship("Attachment", back_populates="transaction")
    edit_requests = db.relationship("TransactionEditRequest", back_populates="transaction")


class TransactionEditRequest(db.Model):
    __tablename__ = "transaction_edit_requests"

    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(
        db.Integer,
        db.ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = db.Column(db.String(16), nullable=False, default="open")  # open/executed/cancelled

    new_type = db.Column(db.String(16), nullable=False)
    new_amount_cents = db.Column(db.BigInteger, nullable=False)
    new_occur_date = db.Column(db.Date, nullable=False)
    new_settled = db.Column(db.Boolean, nullable=False)
    new_counterparty = db.Column(db.String(256), nullable=True)
    new_note = db.Column(db.Text, nullable=True)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    executed_at = db.Column(db.DateTime, nullable=True)
    executed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    transaction = db.relationship("Transaction", back_populates="edit_requests")
    project = db.relationship("Project")
    approvals = db.relationship("TransactionEditApproval", back_populates="request")


class TransactionEditApproval(db.Model):
    __tablename__ = "transaction_edit_approvals"

    request_id = db.Column(
        db.Integer,
        db.ForeignKey("transaction_edit_requests.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    approved_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    request = db.relationship("TransactionEditRequest", back_populates="approvals")
    user = db.relationship("User")


class Attachment(db.Model):
    __tablename__ = "attachments"

    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(
        db.Integer, db.ForeignKey("transactions.id"), nullable=False, index=True
    )
    filename = db.Column(db.String(256), nullable=False)
    stored_path = db.Column(db.String(512), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    uploaded_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    transaction = db.relationship("Transaction", back_populates="attachments")

