from __future__ import annotations

from datetime import date

from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    PasswordField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import DataRequired, EqualTo, Length, NumberRange, Optional
from wtforms import SelectMultipleField

from .project_finance import (
    BROKER_DIR_NET_FROM_BROKER,
    BROKER_DIR_WE_PAY,
    BROKER_MODE_FIXED,
    BROKER_MODE_PERCENT,
)


class LoginForm(FlaskForm):
    username = StringField("用户名", validators=[DataRequired(), Length(max=64)])
    password = PasswordField("密码", validators=[DataRequired(), Length(max=128)])


class ProjectForm(FlaskForm):
    name = StringField("项目名称", validators=[DataRequired(), Length(max=256)])
    leader_user_id = SelectField("负责人", coerce=int, validators=[DataRequired()])
    planned_start_date = DateField("计划开始日期", validators=[DataRequired()])
    planned_end_date = DateField("计划结束日期（终止时间）", validators=[DataRequired()])
    expected_income_yuan = DecimalField(
        "项目合同总金额（元，全额）",
        places=2,
        rounding=None,
        validators=[DataRequired(), NumberRange(min=0)],
    )
    referral_ratio_percent = DecimalField(
        "中介比例 B（%，占客户合同全额）",
        places=4,
        rounding=None,
        validators=[Optional(), NumberRange(min=0, max=100)],
    )
    broker_fee_mode = SelectField(
        "中介分成方式",
        choices=[
            (BROKER_MODE_PERCENT, "按比例（%）"),
            (BROKER_MODE_FIXED, "固定金额（项目总中介费用）"),
        ],
        validators=[DataRequired()],
    )
    broker_fee_direction = SelectField(
        "中介款项方式",
        choices=[
            (BROKER_DIR_NET_FROM_BROKER, "中介从客户款扣除（流水记净额，合同=我方口径）"),
            (BROKER_DIR_WE_PAY, "我方另付介绍费（流水记客户全额）"),
        ],
        validators=[DataRequired()],
    )
    broker_fixed_fee_yuan = DecimalField(
        "中介固定费用（元，总额）",
        places=2,
        rounding=None,
        validators=[Optional(), NumberRange(min=0)],
    )
    status = SelectField(
        "状态", choices=[("open", "进行中"), ("closed", "已结束")], validators=[DataRequired()]
    )
    note = TextAreaField("备注", validators=[Optional(), Length(max=2000)])
    member_user_ids = SelectMultipleField("项目成员", coerce=int, validators=[Optional()])


class ProjectEndDateChangeForm(FlaskForm):
    new_end_date = DateField("新的计划结束日期", validators=[DataRequired()])


class ProjectUpdateForm(FlaskForm):
    body = TextAreaField("进展内容", validators=[DataRequired(), Length(min=1, max=20000)])


class ProjectDividendForm(FlaskForm):
    recipient_key = SelectField(
        "分红对象", coerce=str, validators=[Optional()], validate_choice=False
    )
    new_recipient_name = StringField(
        "新增分红对象",
        validators=[Optional(), Length(max=128)],
        render_kw={"placeholder": "输入后将加入下拉"},
    )
    amount_yuan = DecimalField(
        "本次分红金额（元）",
        places=2,
        rounding=None,
        validators=[DataRequired(), NumberRange(min=0.01)],
    )
    note = TextAreaField("备注", validators=[Optional(), Length(max=2000)])


class TransactionForm(FlaskForm):
    type = SelectField(
        "类型", choices=[("income", "收入"), ("expense", "支出")], validators=[DataRequired()]
    )
    project_id = SelectField(
        "所属项目",
        coerce=int,
        validators=[
            DataRequired(message="必须选择项目"),
            NumberRange(min=1, message="必须选择有效项目"),
        ],
    )
    amount_yuan = DecimalField(
        "金额（元）", places=2, rounding=None, validators=[DataRequired(), NumberRange(min=0.01)]
    )
    occur_date = DateField("发生日期", default=date.today, validators=[DataRequired()])
    settled = BooleanField("已到账/已付款", default=True)
    counterparty = StringField("对方", validators=[Optional(), Length(max=256)])
    note = TextAreaField("备注", validators=[Optional(), Length(max=2000)])


class TransactionEditForm(FlaskForm):
    type = SelectField(
        "类型", choices=[("income", "收入"), ("expense", "支出")], validators=[DataRequired()]
    )
    amount_yuan = DecimalField(
        "金额（元）", places=2, rounding=None, validators=[DataRequired(), NumberRange(min=0.01)]
    )
    occur_date = DateField("发生日期", default=date.today, validators=[DataRequired()])
    settled = BooleanField("已到账/已付款", default=True)
    counterparty = StringField("对方", validators=[Optional(), Length(max=256)])
    note = TextAreaField("备注", validators=[Optional(), Length(max=2000)])


class ProjectAdjustmentForm(FlaskForm):
    amount_yuan = DecimalField(
        "追加应收（元）", places=2, rounding=None, validators=[DataRequired(), NumberRange(min=0.01)]
    )
    note = TextAreaField("备注", validators=[Optional(), Length(max=2000)])


class UserCreateForm(FlaskForm):
    username = StringField("用户名", validators=[DataRequired(), Length(min=2, max=64)])
    role = SelectField(
        "角色",
        choices=[("viewer", "只读"), ("admin", "管理员")],
        validators=[DataRequired()],
    )
    is_active = BooleanField("启用", default=True)
    password = PasswordField(
        "初始密码（可留空）",
        validators=[Optional(), Length(min=6, max=128)],
    )


class UserEditForm(FlaskForm):
    role = SelectField(
        "角色",
        choices=[("viewer", "只读"), ("admin", "管理员")],
        validators=[DataRequired()],
    )
    is_active = BooleanField("启用", default=True)


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("当前密码", validators=[DataRequired(), Length(max=128)])
    new_password = PasswordField("新密码", validators=[DataRequired(), Length(min=6, max=128)])
    new_password2 = PasswordField(
        "确认新密码",
        validators=[DataRequired(), EqualTo("new_password", message="两次输入的新密码不一致")],
    )

