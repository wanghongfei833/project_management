from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json

from sqlalchemy.orm import joinedload

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..utils import format_cents
from ..project_finance import (
    BROKER_DIR_NET_FROM_BROKER,
    BROKER_DIR_WE_PAY,
    BROKER_MODE_FIXED,
    BROKER_MODE_PERCENT,
    build_project_finance,
    referral_ratio_dec,
    running_settled_cash_balance_by_transaction_id,
)
from ..forms import (
    ChangePasswordForm,
    CP_EXPENSE_BROKER,
    CP_EXPENSE_MEMBER,
    CP_EXPENSE_OTHER,
    CP_EXPENSE_REFUND,
    CP_EXPENSE_REIMBURSE,
    CP_EXPENSE_SUPPLIER,
    CP_INCOME_OTHER,
    CP_INCOME_OWNER,
    EXPENSE_CP_CHOICES,
    INCOME_CP_CHOICES,
    LoginForm,
    ProjectAdjustmentForm,
    ProjectDividendForm,
    ProjectEndDateChangeForm,
    ProjectForm,
    ProjectUpdateForm,
    TransactionEditForm,
    TransactionForm,
    UserCreateForm,
    UserEditForm,
)
from ..models import (
    Attachment,
    Project,
    ProjectActivityLog,
    ProjectAdjustmentApproval,
    ProjectExpectedIncomeAdjustment,
    ProjectMember,
    ProjectDeleteApproval,
    ProjectDeleteRequest,
    ProjectLogRule,
    ProjectDividendDistribution,
    ProjectDividendRecipient,
    ProjectEndApproval,
    ProjectEndDateChangeApproval,
    ProjectEndDateChangeRequest,
    ProjectEndRequest,
    ProjectReviveApproval,
    ProjectReviveRequest,
    ProjectUpdate,
    ProjectUpdateAttachment,
    Role,
    SmsNotification,
    SmsNotificationProject,
    Transaction,
    TransactionCreateApproval,
    TransactionCreateRequest,
    TransactionDeleteApproval,
    TransactionDeleteRequest,
    TransactionEditApproval,
    TransactionEditRequest,
    TransactionType,
    User,
)
from ..upload_paths import (
    attachment_display_name,
    project_update_attachment_relpath,
    transaction_attachment_relpath,
)
from ..pending import pending_items_for_user
from ..dividends import dividend_metrics
from ..sms_delivery import sync_delivery_status
from ..log_rules import (
    TZ_OFFSET,
    WEEKDAY_LABELS,
    pending_log_tasks_for_user,
    rules_for_project,
    weekdays_label,
    weekdays_to_csv,
)
from ..sms import ERROR_HINTS, error_hint, is_valid_phone, normalize_phone
from ..sms_alerts import send_pending_alerts, send_test_sms, sms_status
from ..utils import safe_join_upload, sha256_file

# 服务层：跨模块共享逻辑（权限 / 审批判定 / 项目口径 / 金额工具 / 附件）
from ..services.access import *  # noqa: F401,F403
from ..services.approvals import *  # noqa: F401,F403
from ..services.projects import *  # noqa: F401,F403
from ..services.money import *  # noqa: F401,F403
from ..services.attachments import *  # noqa: F401,F403

# 蓝图对象：所有路由都注册在名为 main 的蓝图里，url_for('main.xxx') 保持不变
from . import bp  # noqa: E402,F401


DEFAULT_NEW_USER_PASSWORD = "123456!"


__all__ = [n for n in dir() if not n.startswith("__")]
