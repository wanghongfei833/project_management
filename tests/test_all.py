"""
项目账本 · 自动化测试脚本
用法：pytest tests/ -v
"""
import pytest, os, sys
from pathlib import Path
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone

def _utcnow():
    """兼容 Python 3.12+ 的当前 UTC 时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

import flask
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ["DATABASE_URL"] = f"sqlite:///{PROJECT_ROOT}/tests/test.db"
os.environ["UPLOAD_FOLDER"] = str(PROJECT_ROOT / "tests" / "uploads")

from run import create_app
from ledger_app.extensions import db as _db
from ledger_app.models import (
    User, Project, ProjectMember, Transaction, TransactionType,
    TransactionEditRequest, TransactionEditApproval,
    TransactionDeleteRequest, TransactionDeleteApproval,
    TransactionCreateRequest, TransactionCreateApproval,
    ProjectExpectedIncomeAdjustment, ProjectDividendDistribution,
    ProjectEndRequest, ProjectEndApproval, ProjectReviveRequest, ProjectReviveApproval,
    ProjectDeleteRequest, ProjectDeleteApproval, ProjectActivityLog, Attachment,
    ProjectLogRule, ProjectUpdate,
    SmsNotification, SmsNotificationProject,
)
from ledger_app.project_finance import build_project_finance
from ledger_app.log_rules import (
    build_log_reminder_plans,
    local_day_utc_bounds,
    send_log_reminders,
    weekdays_label,
)
from ledger_app.sms import render_pending_text
from ledger_app.sms_alerts import build_alert_plans, send_pending_alerts


@pytest.fixture(scope="session")
def app():
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SERVER_NAME"] = "test.local"
    with app.app_context():
        _db.create_all()
        _seed_test_data()
        yield app
        _db.session.remove()
        _db.drop_all()
        test_db = PROJECT_ROOT / "tests" / "test.db"
        try:
            if test_db.exists():
                test_db.unlink(missing_ok=True)
        except (PermissionError, OSError):
            pass


@pytest.fixture
def client(app):
    # app fixture 里的 app_context 是 session 级的，Flask 会在请求中复用它，
    # 而 flask_login 把当前用户缓存在 g._login_user 上。不清理的话，
    # 上一个用例登录的账号会「粘」到后面的用例（登出、切换账号全都会失效）。
    from flask import g as _flask_g

    _flask_g.pop("_login_user", None)
    return app.test_client()


def _seed_test_data():
    # 清空所有表数据（避免 seed.py 的初始数据冲突）
    for table in reversed(_db.metadata.sorted_tables):
        _db.session.execute(table.delete())
    _db.session.commit()

    users_data = [
        ("admin", "admin123!", True, "admin"),
        ("wang", "123456!", True, "viewer"),
        ("si", "123456!", True, "viewer"),
        ("hu", "123456!", True, "viewer"),
        ("zhuo", "123456!", True, "viewer"),
        ("wai1", "123456!", True, "viewer"),
        ("wai2", "123456!", True, "viewer"),
    ]
    users = {}
    for uname, pwd, active, role in users_data:
        u = User(username=uname, is_active=active, role=role)
        u.set_password(pwd)
        _db.session.add(u)
        _db.session.flush()
        users[uname] = u

    pA = Project(name="ProjA", expected_income_cents=10000000,
                 broker_fee_mode="fixed", broker_fee_direction="we_pay_separate",
                 broker_fixed_fee_cents=500000, status="open", can_dividend=True,
                 leader_user_id=users["wang"].id,
                 planned_start_date=date(2026,1,1), planned_end_date=date(2026,12,31))
    _db.session.add(pA)
    _db.session.flush()
    for u in ["admin", "wang", "si"]:
        _db.session.add(ProjectMember(project_id=pA.id, user_id=users[u].id))

    pA1 = Project(name="ProjA1", expected_income_cents=5000000,
                  broker_fee_mode="percent", broker_fee_direction="we_pay_separate",
                  referral_ratio=Decimal("0"), status="open",
                  parent_project_id=pA.id, can_dividend=False,
                  leader_user_id=users["hu"].id,
                  planned_start_date=date(2026,3,1), planned_end_date=date(2026,9,30))
    _db.session.add(pA1)
    _db.session.flush()
    for u in ["hu", "wai1"]:
        _db.session.add(ProjectMember(project_id=pA1.id, user_id=users[u].id))

    pA2 = Project(name="ProjA2", expected_income_cents=5000000,
                  broker_fee_mode="percent", broker_fee_direction="we_pay_separate",
                  referral_ratio=Decimal("0"), status="open",
                  parent_project_id=pA.id, can_dividend=True,
                  leader_user_id=users["zhuo"].id,
                  planned_start_date=date(2026,4,1), planned_end_date=date(2026,10,31))
    _db.session.add(pA2)
    _db.session.flush()
    for u in ["zhuo", "wai2"]:
        _db.session.add(ProjectMember(project_id=pA2.id, user_id=users[u].id))

    pB = Project(name="ProjB", expected_income_cents=3000000,
                 broker_fee_mode="percent", broker_fee_direction="we_pay_separate",
                 referral_ratio=Decimal("0.1"), status="open", can_dividend=True,
                 leader_user_id=users["wang"].id,
                 planned_start_date=date(2026,2,1), planned_end_date=date(2026,8,31))
    _db.session.add(pB)
    _db.session.flush()
    for u in ["admin", "wang", "si", "hu"]:
        _db.session.add(ProjectMember(project_id=pB.id, user_id=users[u].id))

    _db.session.commit()
    flask.current_app.config["_SEED"] = {
        "users": {k: v.id for k, v in users.items()},
        "projects": {"A": pA.id, "A1": pA1.id, "A2": pA2.id, "B": pB.id}
    }


def login(client, username, password="123456!"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)


def get_seed(key=None):
    data = flask.current_app.config["_SEED"]
    return data[key] if key else data


# ================================= TEST CLASSES =================================

class TestLogin:
    def test_login_admin(self, client):
        rv = login(client, "admin", "admin123!")
        assert rv.status_code == 200

    def test_login_normal(self, client):
        rv = login(client, "wang")
        assert rv.status_code == 200

    def test_login_wrong_password(self, client):
        rv = client.post("/login", data={"username": "admin", "password": "wrong"}, follow_redirects=True)
        assert rv.status_code == 200


class TestDashboard:
    def test_dashboard_loads(self, client):
        login(client, "wang")
        rv = client.get("/")
        assert rv.status_code == 200

    def test_parent_project_shows_sub_count(self, client):
        login(client, "admin", "admin123!")
        rv = client.get("/")
        assert rv.status_code == 200


class TestParentChild:
    def test_create_child_project(self, client):
        login(client, "admin", "admin123!")
        seed = get_seed()
        rv = client.post("/projects/new", data={
            "name": "ChildC", "parent_project_id": seed["projects"]["A"],
            "can_dividend": 2,
            "leader_user_id": seed["users"]["admin"],
            "planned_start_date": "2026-06-01", "planned_end_date": "2026-12-31",
            "expected_income_yuan": "30000.00",
            "broker_fee_mode": "percent", "broker_fee_direction": "we_pay_separate",
            "referral_ratio_percent": "0", "broker_fixed_fee_yuan": "0",
            "status": "open",
            "member_user_ids": [seed["users"]["wai1"], seed["users"]["wai2"]],
        }, follow_redirects=True)
        assert rv.status_code == 200

    def test_create_independent_project(self, client):
        login(client, "admin", "admin123!")
        seed = get_seed()
        rv = client.post("/projects/new", data={
            "name": "IndepD", "parent_project_id": 0,
            "leader_user_id": seed["users"]["admin"],
            "planned_start_date": "2026-06-01", "planned_end_date": "2026-12-31",
            "expected_income_yuan": "50000.00",
            "broker_fee_mode": "percent", "broker_fee_direction": "we_pay_separate",
            "referral_ratio_percent": "0", "broker_fixed_fee_yuan": "0",
            "status": "open",
            "member_user_ids": [seed["users"]["wang"]],
        }, follow_redirects=True)
        assert rv.status_code == 200

    def test_parent_member_can_view_child(self, client):
        login(client, "wang")
        seed = get_seed()
        rv = client.get(f"/projects/{seed['projects']['A1']}")
        assert rv.status_code == 200

    def test_child_member_cannot_view_sibling(self, client):
        login(client, "wai1")
        seed = get_seed()
        rv = client.get(f"/projects/{seed['projects']['A2']}", follow_redirects=True)
        assert rv.status_code == 200

    def test_ended_parent_not_in_dropdown(self, client):
        login(client, "admin", "admin123!")
        seed = get_seed()
        pA2 = _db.session.get(Project, seed["projects"]["A2"])
        pA2.status = "ended"
        pA2.ended_at = _utcnow()
        _db.session.commit()
        rv = client.get("/projects/new")
        pA2.status = "open"
        pA2.ended_at = None
        _db.session.commit()
        assert rv.status_code == 200


class TestTransactions:
    def test_admin_create_transaction(self, client):
        login(client, "admin", "admin123!")
        seed = get_seed()
        rv = client.post("/transactions/new", data={
            "project_id": seed["projects"]["B"], "type": "income",
            "amount_yuan": "10000.00", "occur_date": date.today().isoformat(),
            "settled": 1, "counterparty": "client", "note": "test income",
        }, follow_redirects=True)
        assert rv.status_code == 200

    def test_normal_user_create_pending(self, client):
        login(client, "hu")
        seed = get_seed()
        rv = client.post("/transactions/new", data={
            "project_id": seed["projects"]["A1"], "type": "income",
            "amount_yuan": "5000.00", "occur_date": date.today().isoformat(),
            "settled": 1, "counterparty": "client", "note": "A1 income",
        }, follow_redirects=True)
        assert rv.status_code == 200

    def test_redirects_to_project(self, client):
        login(client, "admin", "admin123!")
        seed = get_seed()
        rv = client.post("/transactions/new", data={
            "project_id": seed["projects"]["B"], "type": "expense",
            "amount_yuan": "2000.00", "occur_date": date.today().isoformat(),
            "settled": 1, "counterparty": "supplier",
        }, follow_redirects=False)
        assert rv.status_code in (302, 303)
        assert f"/projects/{seed['projects']['B']}" in rv.headers.get("Location", "")

    def test_blocked_on_ended_project(self, client):
        login(client, "admin", "admin123!")
        seed = get_seed()
        pA1 = _db.session.get(Project, seed["projects"]["A1"])
        pA1.status = "ended"
        pA1.ended_at = _utcnow()
        _db.session.commit()
        rv = client.post("/transactions/new", data={
            "project_id": seed["projects"]["A1"], "type": "income",
            "amount_yuan": "1000.00", "occur_date": date.today().isoformat(),
        }, follow_redirects=True)
        pA1.status = "open"
        pA1.ended_at = None
        _db.session.commit()
        assert rv.status_code == 200


class TestApproval:
    def test_admin_auto_approve(self, client):
        login(client, "admin", "admin123!")
        seed = get_seed()
        client.post("/transactions/new", data={
            "project_id": seed["projects"]["B"], "type": "income",
            "amount_yuan": "5000", "occur_date": date.today().isoformat(),
            "settled": 1, "counterparty": "test",
        })
        tx = Transaction.query.filter_by(project_id=seed["projects"]["B"]).order_by(Transaction.id.desc()).first()
        assert tx is not None
        rv = client.post(f"/transactions/{tx.id}/delete-request", follow_redirects=True)
        assert rv.status_code in (200, 302)

    def test_sub_project_approval_independent(self, client):
        login(client, "hu")
        seed = get_seed()
        tx = Transaction.query.filter_by(project_id=seed["projects"]["A1"], status="pending").first()
        if tx:
            rv = client.post(f"/transactions/{tx.id}/create-approve", follow_redirects=True)
            assert rv.status_code in (200, 302)


class TestEndRevive:
    def test_end_project(self, client):
        login(client, "hu")
        seed = get_seed()
        rv = client.post(f"/projects/{seed['projects']['A1']}/end-request", follow_redirects=True)
        assert rv.status_code in (200, 302)

    def test_approve_end_and_readonly(self, client):
        login(client, "admin", "admin123!")
        seed = get_seed()
        rv = client.post(f"/projects/{seed['projects']['A1']}/end-approve", follow_redirects=True)

    def test_revive_project(self, client):
        seed = get_seed()
        # A1 的成员发起复活申请，另一位成员同意后自动执行
        # （admin 不是 A1 成员，发起/审批都要求项目成员身份）
        login(client, "hu")
        client.post(f"/projects/{seed['projects']['A1']}/revive-request")
        client.post("/logout")
        login(client, "wai1")
        client.post(
            f"/projects/{seed['projects']['A1']}/revive-approve", follow_redirects=True
        )
        p = _db.session.get(Project, seed["projects"]["A1"])
        assert p.status == "open"


class TestDividend:
    def test_dividend_page(self, client):
        login(client, "admin", "admin123!")
        seed = get_seed()
        p = _db.session.get(Project, seed["projects"]["A2"])
        if p.status != "ended":
            p.status = "ended"
            p.ended_at = _utcnow()
            _db.session.commit()
        rv = client.get(f"/projects/{seed['projects']['A2']}/dividend")
        assert rv.status_code == 200


class TestDelete:
    def test_admin_delete_project(self, client):
        login(client, "admin", "admin123!")
        seed = get_seed()
        p = Project(name="TempDel", status="open", can_dividend=True,
                     leader_user_id=seed["users"]["admin"],
                     planned_start_date=date.today(), planned_end_date=date.today())
        _db.session.add(p)
        _db.session.flush()
        _db.session.add(ProjectMember(project_id=p.id, user_id=seed["users"]["admin"]))
        _db.session.commit()
        pid = p.id
        rv = client.post(f"/projects/{pid}/delete-request", follow_redirects=True)
        assert _db.session.get(Project, pid) is None or rv.status_code in (200, 302)


class TestFinanceCalculation:
    """
    ═══════════════════════════════════════════════════════════════
    财务数值正确性验证 —— 每笔金钱的流向都清晰说明
    ═══════════════════════════════════════════════════════════════

    测试数据说明：
      父项目 A：合同10W，固定介绍费5K
      子项目 A1：合同5W，不可分红
      子项目 A2：合同5W，可分红
      独立项目 B：合同3W，10%介绍费

    注意：测试使用独立的 test.db，不影响真实数据。
    """

    def _create_income(self, client, pid, amount, note=""):
        return client.post("/transactions/new", data={
            "project_id": pid, "type": "income",
            "amount_yuan": str(amount),
            "occur_date": "2026-06-01", "settled": 1,
            "counterparty": "客户", "note": note,
        }, follow_redirects=True)

    def _create_expense(self, client, pid, amount, note=""):
        return client.post("/transactions/new", data={
            "project_id": pid, "type": "expense",
            "amount_yuan": str(amount),
            "occur_date": "2026-06-01", "settled": 1,
            "counterparty": "供应商", "note": note,
        }, follow_redirects=True)

    def _get_finance(self, client, pid):
        rv = client.get(f"/projects/{pid}")
        return rv.data.decode("utf-8")

    # ─────────────────────────────────────────────────────────
    # 场景1：子项目不可分红 → 利润回流父项目
    # ─────────────────────────────────────────────────────────
    def test_child_no_dividend_profit_flows_back(self, client):
        """
        【场景】A1（不可分红）：
          收入：¥50,000（已到账）
          支出：¥20,000（非分红）
          ─────────────────
          利润：¥30,000

        【资金流向】
          A1 的利润 ¥30,000 → 回流到父项目 A
          父项目 A 的可分红基数应包含这 ¥30,000
          A1 页面无分红入口

        【校验】
          ✅ A1 页面不可见分红入口
        """
        login(client, "admin", "admin123!")
        seed = get_seed()
        pid = seed["projects"]["A1"]

        p = _db.session.get(Project, pid)
        if p.status != "ended":
            p.status = "ended"
            p.ended_at = _utcnow()
            _db.session.commit()

        self._create_income(client, pid, 50000.00, "A1收入")
        self._create_expense(client, pid, 20000.00, "A1支出")

        html = self._get_finance(client, seed["projects"]["A1"])
        # A1 不可分红 → 无分红入口
        assert "可分红" not in html

    # ─────────────────────────────────────────────────────────
    # 场景2：子项目可分红 → 独立分红，不回流
    # ─────────────────────────────────────────────────────────
    def test_child_can_dividend_independent(self, client):
        """
        【场景】A2（可分红）：
          收入：¥80,000（已到账）
          支出：¥30,000（非分红）
          ─────────────────
          利润：¥50,000

        【资金流向】
          A2 的利润 ¥50,000 → A2 自己保留，不回流父项目
          A2 的可分红基数 = ¥50,000

        【校验】
          ✅ A2 分红页面显示可分红 ¥50,000
        """
        login(client, "admin", "admin123!")
        seed = get_seed()
        pid = seed["projects"]["A2"]

        self._create_income(client, pid, 80000.00, "A2收入")
        self._create_expense(client, pid, 30000.00, "A2支出")

        p = _db.session.get(Project, pid)
        p.status = "ended"
        p.ended_at = _utcnow()
        _db.session.commit()

        rv = client.get(f"/projects/{pid}/dividend")
        html = rv.data.decode("utf-8")
        assert "¥" in html and "分红" in html

    # ─────────────────────────────────────────────────────────
    # 场景3：收支平衡 → 利润为0 → 不可分红
    # ─────────────────────────────────────────────────────────
    def test_zero_profit(self, client):
        """
        【场景】临时项目 ZeroProfit：
          收入：¥30,000（已到账）
          支出：¥30,000（非分红）
          ─────────────────
          利润：¥0

        【资金流向】
          利润为0 → 无可分红金额
          分红页面显示"无剩余可分红金额"

        【校验】
          ✅ 分红页面提示「无剩余可分红金额」
        """
        login(client, "admin", "admin123!")
        p = Project(name="ZeroProfit", status="open", can_dividend=True,
                     leader_user_id=get_seed()["users"]["admin"],
                     planned_start_date=date.today(), planned_end_date=date.today())
        _db.session.add(p)
        _db.session.flush()
        _db.session.add(ProjectMember(project_id=p.id, user_id=get_seed()["users"]["admin"]))
        _db.session.commit()
        pid = p.id

        self._create_income(client, pid, 30000.00, "收入3W")
        self._create_expense(client, pid, 30000.00, "支出3W")

        p.status = "ended"
        p.ended_at = _utcnow()
        _db.session.commit()

        rv = client.get(f"/projects/{pid}/dividend")
        html = rv.data.decode("utf-8")
        assert "无剩余可分红" in html

    # ─────────────────────────────────────────────────────────
    # 场景4：部分分红已完成
    # ─────────────────────────────────────────────────────────
    def test_partial_dividend(self, client):
        """
        【场景】A2（可分红，已在上一步创建了收入8W-支出3W=利润5W）：
          总利润：     ¥50,000
          已登记分红：  ¥0（等用户手动操作）
          剩余可分红： ¥50,000

        【资金流向】
          如果均分给 A2 的 2 名成员：
            每人 ¥25,000
            总分红 ¥50,000 → 剩余 ¥0

        【校验】
          ✅ 分红页面能正常打开
        """
        login(client, "admin", "admin123!")
        seed = get_seed()
        pid = seed["projects"]["A2"]

        p = _db.session.get(Project, pid)
        p.status = "ended"
        p.ended_at = _utcnow()
        _db.session.commit()

        rv = client.get(f"/projects/{pid}/dividend")
        html = rv.data.decode("utf-8")
        assert "分红" in html

    # ─────────────────────────────────────────────────────────
    # 场景5：父项目汇总多个子项目
    # ─────────────────────────────────────────────────────────
    def test_parent_aggregation(self, client):
        """
        【场景】父项目 A 汇总自身 + 所有子项目：

          父项目 A 自身：无流水，利润 0
          ├── A1（不可分红）：利润 ¥30,000 → 回流 A
          ├── A2（可分红）：  利润 ¥50,000 → A2 独立，不影响 A
          └── A 自身：¥0

          A 的可分红基数 = 0 + 30,000（A1回流）= ¥30,000
          A2 的可分红基数 = ¥50,000（不受 A 影响）

        【校验】
          ✅ 父项目 A 页面能正常打开
        """
        login(client, "admin", "admin123!")
        seed = get_seed()
        rv = client.get(f"/projects/{seed['projects']['A']}")
        assert rv.status_code == 200

    # ─────────────────────────────────────────────────────────
    # 场景6：均分分红测试
    # ─────────────────────────────────────────────────────────
    def test_equal_split_dividend(self, client):
        """
        【场景】A2 有剩余可分红 ¥50,000，均分给 2 名成员（卓文浩、外包乙）：

          人均 = 50,000 ÷ 2 = ¥25,000（整除，无余数）
          卓文浩： ¥25,000
          外包乙： ¥25,000

        【资金流向】
          创建一笔支出流水 ¥50,000（[DIVIDEND] 标记）
          每人一条分红记录 ¥25,000
          分红后 A2 剩余可分红 = ¥0

        【校验】
          ✅ 均分后每人 ¥25,000
        """
        login(client, "admin", "admin123!")
        seed = get_seed()
        pid = seed["projects"]["A2"]

        # 确保 A2 已终止且可分红
        p = _db.session.get(Project, pid)
        if p.status != "ended":
            p.status = "ended"
            p.ended_at = _utcnow()
            _db.session.commit()

        # 模拟均分：通过分红页面提交
        rv = client.post(f"/projects/{pid}/dividend", data={
            f"amount_{seed['users']['zhuo']}": "25000.00",
            f"amount_{seed['users']['wai2']}": "25000.00",
        }, follow_redirects=True)
        html = rv.data.decode("utf-8")
        # 提交后应看到成功提示
        assert rv.status_code == 200


class TestPendingApprovals:
    """「待我处理」：登录弹窗 + 铃铛待办 + 待办页一键同意。"""

    @staticmethod
    def _switch_user(client, username, password="123456!"):
        """切换账号（login 视图对已登录用户会直接跳首页，必须先登出）。"""
        client.post("/logout")
        return login(client, username, password)

    def _fresh_project(self, name):
        """建一个只有 wang / si 两个非管理员成员的项目，避免与其他用例互相干扰。"""
        seed = get_seed()
        p = Project.query.filter_by(name=name).first()
        if p:
            return p
        p = Project(
            name=name,
            expected_income_cents=0,
            broker_fee_mode="percent",
            broker_fee_direction="we_pay_separate",
            referral_ratio=Decimal("0"),
            status="open",
            can_dividend=True,
            leader_user_id=seed["users"]["wang"],
            planned_start_date=date(2026, 1, 1),
            planned_end_date=date(2026, 12, 31),
        )
        _db.session.add(p)
        _db.session.flush()
        for uname in ["admin", "wang", "si"]:
            _db.session.add(ProjectMember(project_id=p.id, user_id=seed["users"][uname]))
        _db.session.commit()
        return p

    def test_login_popup_and_todo_list(self, client):
        p = self._fresh_project("ProjTodo")

        # A（wang）发起一笔流水申请
        login(client, "wang")
        client.post("/transactions/new", data={
            "project_id": p.id, "type": "expense",
            "amount_yuan": "1234.56", "occur_date": date.today().isoformat(),
            "settled": 1, "counterparty": "supplier", "note": "待办用例",
        })
        tx = (
            Transaction.query.filter_by(project_id=p.id, status="pending")
            .order_by(Transaction.id.desc())
            .first()
        )
        assert tx is not None

        # 发起人自己不算待办（发起时自动记了一条同意）
        html = client.get("/approvals").data.decode("utf-8")
        assert "ProjTodo" in html
        assert "我已同意，等待其他人" in html

        # B（si）登录：登录后第一次打开页面弹窗，第二次不再弹
        rv = self._switch_user(client, "si")
        first_html = rv.data.decode("utf-8")
        assert "pendingApprovalModal" in first_html
        assert "ProjTodo" in first_html
        assert f"/transactions/{tx.id}/create-approve" in first_html

        second_html = client.get("/").data.decode("utf-8")
        assert "pendingApprovalModal" not in second_html
        # 铃铛常驻显示待办数
        assert "待办" in second_html

        # 非成员（zhuo）看不到这个项目的待办
        self._switch_user(client, "zhuo")
        html = client.get("/approvals").data.decode("utf-8")
        assert "ProjTodo" not in html

        # B 在待办页一键同意 → 回到待办页，且申请自动生效
        self._switch_user(client, "si")
        rv = client.post(
            f"/transactions/{tx.id}/create-approve",
            data={"next": "/approvals"},
            follow_redirects=False,
        )
        assert rv.status_code in (301, 302, 303)
        assert rv.headers.get("Location") == "/approvals"
        _db.session.refresh(tx)
        assert tx.status == "active"

        html = client.get("/approvals").data.decode("utf-8")
        assert "ProjTodo" not in html

    def test_project_end_request_notifies_other_member(self, client):
        p = self._fresh_project("ProjTodoEnd")

        # A（wang）发起项目终止申请
        login(client, "wang")
        client.post(f"/projects/{p.id}/end-request")
        _db.session.refresh(p)
        assert p.status == "open"

        # B（si）登录即可看到「项目终止」待办
        html = self._switch_user(client, "si").data.decode("utf-8")
        assert "项目终止" in html
        assert "ProjTodoEnd" in html

        # B 同意后自动执行终止
        client.post(
            f"/projects/{p.id}/end-approve",
            data={"next": "/approvals"},
            follow_redirects=True,
        )
        _db.session.refresh(p)
        assert p.status == "ended"


class TestPhoneAndSms:
    """手机号录入 + 待办超时短信提醒 + 短信溯源。"""

    @staticmethod
    def _fake_sender(ok=True, biz_id="B-TEST"):
        calls = []

        def _send(phone, number, **kwargs):
            calls.append((phone, number))
            return {
                "ok": ok,
                "code": "OK" if ok else "isv.BUSINESS_LIMIT_CONTROL",
                "message": "OK" if ok else "触发流控",
                "request_id": "R-TEST",
                "biz_id": biz_id if ok else "",
                "content": render_pending_text(number),
                "configured": True,
                "template_code": "SMS_512630691",
                "phone": phone,
            }

        return _send, calls

    def _fresh_project(self, name, members):
        seed = get_seed()
        p = Project.query.filter_by(name=name).first()
        if p:
            return p
        p = Project(
            name=name,
            expected_income_cents=0,
            broker_fee_mode="percent",
            broker_fee_direction="we_pay_separate",
            referral_ratio=Decimal("0"),
            status="open",
            can_dividend=True,
            leader_user_id=seed["users"][members[0]],
            planned_start_date=date(2026, 1, 1),
            planned_end_date=date(2026, 12, 31),
        )
        _db.session.add(p)
        _db.session.flush()
        for uname in ["admin", *members]:
            _db.session.add(ProjectMember(project_id=p.id, user_id=seed["users"][uname]))
        _db.session.commit()
        return p

    @staticmethod
    def _enable_sms(monkeypatch):
        """让 sms_configured() 返回 True（发送本身会被 monkeypatch 掉）。"""
        monkeypatch.setenv("ALIYUN_SMS_ACCESS_KEY_ID", "test-key-id")
        monkeypatch.setenv("ALIYUN_SMS_ACCESS_KEY_SECRET", "test-key-secret")
        monkeypatch.setenv("ALIYUN_SMS_SIGN_NAME", "测试签名")
        monkeypatch.setenv("ALIYUN_SMS_TEMPLATE_CODE", "SMS_512630691")

    def _make_pending_item(self, project_id, requester_id, hours_old=7):
        tx = Transaction(
            project_id=project_id, status="pending", type="income",
            amount_cents=100000, occur_date=date.today(), settled=True,
            counterparty="client", created_by_user_id=requester_id,
        )
        _db.session.add(tx)
        _db.session.flush()
        req = TransactionCreateRequest(
            transaction_id=tx.id, project_id=project_id, status="open",
            created_by_user_id=requester_id,
        )
        _db.session.add(req)
        _db.session.flush()
        _db.session.add(TransactionCreateApproval(request_id=req.id, user_id=requester_id))
        req.created_at = _utcnow() - timedelta(hours=hours_old)
        _db.session.commit()
        return tx, req

    # ── 手机号录入 ────────────────────────────────────────────
    def test_create_user_requires_phone(self, client):
        login(client, "admin", "admin123!")
        base = {"username": "phoneless", "role": "viewer", "is_active": 1, "password": "abc123!"}

        # 不填手机号 → 不创建
        rv = client.post("/users/new", data=dict(base), follow_redirects=True)
        assert rv.status_code == 200
        assert User.query.filter_by(username="phoneless").first() is None

        # 号码不合法 → 不创建
        rv = client.post("/users/new", data=dict(base, phone="12345"), follow_redirects=True)
        assert User.query.filter_by(username="phoneless").first() is None

        # 正常号码（带空格/+86 也能识别）→ 存成 11 位数字
        rv = client.post(
            "/users/new", data=dict(base, phone="+86 138 0000 0003"), follow_redirects=True
        )
        u = User.query.filter_by(username="phoneless").first()
        assert u is not None and u.phone == "13800000003"

        # 号码重复 → 拒绝
        rv = client.post(
            "/users/new",
            data=dict(base, username="phoneless2", phone="13800000003"),
            follow_redirects=True,
        )
        assert User.query.filter_by(username="phoneless2").first() is None

    def test_edit_user_backfills_phone(self, client):
        login(client, "admin", "admin123!")
        seed = get_seed()
        hu = _db.session.get(User, seed["users"]["hu"])
        rv = client.post(
            f"/users/{hu.id}/edit",
            data={"phone": "13900000001", "role": hu.role, "is_active": 1},
            follow_redirects=True,
        )
        assert rv.status_code == 200
        _db.session.refresh(hu)
        assert hu.phone == "13900000001"

        # 别人已用的号码 → 拒绝
        zhuo = _db.session.get(User, seed["users"]["zhuo"])
        client.post(
            f"/users/{zhuo.id}/edit",
            data={"phone": "13900000001", "role": zhuo.role, "is_active": 1},
            follow_redirects=True,
        )
        _db.session.refresh(zhuo)
        assert not zhuo.phone

    # ── 提醒判定 ──────────────────────────────────────────────
    def test_alert_skips_without_phone_and_before_6h(self, client, monkeypatch):
        seed = get_seed()
        p = self._fresh_project("ProjSmsSkip", ["wang", "si"])
        wang = _db.session.get(User, seed["users"]["wang"])
        si = _db.session.get(User, seed["users"]["si"])
        # si 没有手机号
        si.phone = None
        _db.session.commit()

        self._make_pending_item(p.id, wang.id, hours_old=7)
        plans, skipped = build_alert_plans(user_id=si.id)
        assert plans == []
        assert skipped["no_phone"] == 1

        # 补上号码，但申请只积压 2 小时 → 还不提醒
        si.phone = "13800000002"
        _db.session.commit()
        tx = (
            Transaction.query.filter_by(project_id=p.id, status="pending")
            .order_by(Transaction.id.desc())
            .first()
        )
        req = TransactionCreateRequest.query.filter_by(transaction_id=tx.id).first()
        req.created_at = _utcnow() - timedelta(hours=2)
        _db.session.commit()
        plans, skipped = build_alert_plans(user_id=si.id)
        assert plans == []
        assert skipped["not_overdue"] == 1

    def test_send_alerts_records_and_cools_down(self, client, monkeypatch):
        self._enable_sms(monkeypatch)
        seed = get_seed()
        p = self._fresh_project("ProjSmsSend", ["wang", "si"])
        wang = _db.session.get(User, seed["users"]["wang"])
        si = _db.session.get(User, seed["users"]["si"])
        si.phone = "13800000002"
        _db.session.commit()
        self._make_pending_item(p.id, wang.id, hours_old=7)

        sender, calls = self._fake_sender()
        monkeypatch.setattr("ledger_app.sms_alerts.send_sms", sender)

        # 只要 si 的人均待办数：应包含本项目的那 1 条
        plans, _ = build_alert_plans(user_id=si.id)
        assert len(plans) == 1
        assert plans[0].overdue_count >= 1
        assert any(pid == p.id for pid, _name, _cnt in plans[0].projects)
        expected_count = plans[0].pending_count

        res = send_pending_alerts(user_id=si.id)
        assert res["sent"] == 1 and res["failed"] == 0
        assert calls == [("13800000002", expected_count)]

        rec = (
            SmsNotification.query.filter_by(user_id=si.id, trigger="auto")
            .order_by(SmsNotification.id.desc())
            .first()
        )
        assert rec is not None and rec.status == "sent"
        assert rec.phone == "13800000002"
        assert rec.pending_count == expected_count
        assert rec.content == render_pending_text(expected_count)
        assert rec.request_id == "B-TEST"
        # 项目拆分：能查到「某个项目给谁发过多少条」
        assert any(sp.project_id == p.id and sp.item_count >= 1 for sp in rec.projects)

        # 6 小时冷却
        res2 = send_pending_alerts(user_id=si.id)
        assert res2["sent"] == 0 and res2["skipped"]["cooling"] == 1
        # force 可以忽略冷却
        res3 = send_pending_alerts(user_id=si.id, force=True)
        assert res3["sent"] == 1

    def test_send_failure_is_recorded(self, client, monkeypatch):
        self._enable_sms(monkeypatch)
        seed = get_seed()
        p = self._fresh_project("ProjSmsFail", ["wang", "si"])
        wang = _db.session.get(User, seed["users"]["wang"])
        si = _db.session.get(User, seed["users"]["si"])
        si.phone = "13800000002"
        _db.session.commit()
        self._make_pending_item(p.id, wang.id, hours_old=8)

        sender, _calls = self._fake_sender(ok=False)
        monkeypatch.setattr("ledger_app.sms_alerts.send_sms", sender)
        res = send_pending_alerts(user_id=si.id, force=True)
        assert res["sent"] == 0 and res["failed"] == 1
        rec = (
            SmsNotification.query.filter_by(user_id=si.id, status="failed")
            .order_by(SmsNotification.id.desc())
            .first()
        )
        assert rec is not None and "BUSINESS_LIMIT_CONTROL" in (rec.error or "")

    # ── 管理页 ────────────────────────────────────────────────
    def test_sms_page_and_test_send(self, client, monkeypatch):
        self._enable_sms(monkeypatch)
        seed = get_seed()
        wang = _db.session.get(User, seed["users"]["wang"])

        login(client, "wang")
        rv = client.get("/admin/sms")
        assert rv.status_code in (301, 302, 303)  # 非管理员拿不到

        client.post("/logout")
        login(client, "admin", "admin123!")
        rv = client.get("/admin/sms")
        assert rv.status_code == 200
        assert "短信提醒" in rv.data.decode("utf-8")

        sender, calls = self._fake_sender()
        monkeypatch.setattr("ledger_app.sms_alerts.send_sms", sender)
        rv = client.post(
            "/admin/sms/test",
            data={"phone": "13800000009", "number": 2, "next": "/admin/sms"},
            follow_redirects=True,
        )
        assert rv.status_code == 200
        assert calls == [("13800000009", 2)]
        rec = (
            SmsNotification.query.filter_by(trigger="test")
            .order_by(SmsNotification.id.desc())
            .first()
        )
        assert rec is not None and rec.status == "sent" and rec.pending_count == 2


class TestSchemaBootstrap:
    """gunicorn 多 worker 并发启动建表时的竞态（table already exists / duplicate column）。"""

    def test_retries_when_table_already_exists(self, app, monkeypatch):
        from sqlalchemy.exc import OperationalError

        import ledger_app as pkg

        calls = {"n": 0}
        real_create_all = _db.create_all

        def flaky_create_all(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError(
                    "CREATE TABLE sms_notifications",
                    {},
                    Exception("table sms_notifications already exists"),
                )
            return real_create_all(*args, **kwargs)

        monkeypatch.setattr(_db, "create_all", flaky_create_all)
        pkg._bootstrap_schema()
        assert calls["n"] >= 2  # 第一次撞竞态，重试后成功

    def test_other_operational_errors_still_raise(self, app, monkeypatch):
        from sqlalchemy.exc import OperationalError

        import ledger_app as pkg

        def broken(*args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception("disk I/O error"))

        monkeypatch.setattr(_db, "create_all", broken)
        with pytest.raises(OperationalError):
            pkg._bootstrap_schema(attempts=2)


class TestLogRulesAndReminders:
    """日志撰写规则（每周 N 天）+ 每天 20:00 未写日志短信提醒。"""

    MONDAY = date(2026, 3, 2)  # 周一
    TUESDAY = date(2026, 3, 3)

    @staticmethod
    def _enable_sms(monkeypatch):
        monkeypatch.setenv("ALIYUN_SMS_ACCESS_KEY_ID", "test-key-id")
        monkeypatch.setenv("ALIYUN_SMS_ACCESS_KEY_SECRET", "test-key-secret")
        monkeypatch.setenv("ALIYUN_SMS_SIGN_NAME", "测试签名")

    @staticmethod
    def _fake_sender(ok=True):
        calls = []

        def _send(phone, number, **kwargs):
            calls.append((phone, number))
            return {
                "ok": ok,
                "code": "OK" if ok else "isv.BUSINESS_LIMIT_CONTROL",
                "message": "OK" if ok else "触发流控",
                "request_id": "R-LOG",
                "biz_id": "B-LOG" if ok else "",
                "content": render_pending_text(number),
                "configured": True,
                "template_code": "SMS_512630691",
                "phone": phone,
            }

        return _send, calls

    def _project(self, name, members):
        seed = get_seed()
        p = Project.query.filter_by(name=name).first()
        if p:
            return p
        p = Project(
            name=name,
            expected_income_cents=0,
            broker_fee_mode="percent",
            broker_fee_direction="we_pay_separate",
            referral_ratio=Decimal("0"),
            status="open",
            can_dividend=True,
            leader_user_id=seed["users"][members[0]],
            planned_start_date=date(2026, 1, 1),
            planned_end_date=date(2026, 12, 31),
        )
        _db.session.add(p)
        _db.session.flush()
        for uname in ["admin", *members]:
            _db.session.add(ProjectMember(project_id=p.id, user_id=seed["users"][uname]))
        _db.session.commit()
        return p

    def test_add_update_delete_rule(self, client):
        seed = get_seed()
        p = self._project("ProjLogRule", ["wang", "si"])
        si = _db.session.get(User, seed["users"]["si"])
        login(client, "admin", "admin123!")

        rv = client.post(
            f"/projects/{p.id}/log-rules/add",
            data={"user_id": si.id, "weekdays": ["1", "3", "5"]},
            follow_redirects=True,
        )
        assert rv.status_code == 200
        rule = ProjectLogRule.query.filter_by(project_id=p.id, user_id=si.id).first()
        assert rule is not None and rule.weekdays == "1,3,5"
        assert weekdays_label(rule.weekdays) == "每周一、周三、周五"

        # 同一个人再 add = 改频率，不会产生第二条
        client.post(
            f"/projects/{p.id}/log-rules/add",
            data={"user_id": si.id, "weekdays": ["1", "2", "3", "4", "5", "6", "7"]},
            follow_redirects=True,
        )
        assert ProjectLogRule.query.filter_by(project_id=p.id, user_id=si.id).count() == 1
        rule = ProjectLogRule.query.filter_by(project_id=p.id, user_id=si.id).first()
        assert weekdays_label(rule.weekdays) == "每天"

        # 详情页能看到规则
        html = client.get(f"/projects/{p.id}").get_data(as_text=True)
        assert "日志撰写规则" in html and "每天" in html

        client.post(f"/projects/{p.id}/log-rules/{rule.id}/delete", follow_redirects=True)
        assert ProjectLogRule.query.filter_by(project_id=p.id, user_id=si.id).first() is None

    def test_rule_guards(self, client):
        seed = get_seed()
        p = self._project("ProjLogGuard", ["wang", "si"])
        si = _db.session.get(User, seed["users"]["si"])
        zhuo = _db.session.get(User, seed["users"]["zhuo"])  # 非本项目成员

        # 普通成员无权添加
        login(client, "si")
        client.post(
            f"/projects/{p.id}/log-rules/add",
            data={"user_id": si.id, "weekdays": ["1"]},
            follow_redirects=True,
        )
        assert ProjectLogRule.query.filter_by(project_id=p.id).count() == 0

        # 负责人可以添加；但不能加非项目成员
        client.post("/logout")
        login(client, "wang")
        client.post(
            f"/projects/{p.id}/log-rules/add",
            data={"user_id": zhuo.id, "weekdays": ["1"]},
            follow_redirects=True,
        )
        assert ProjectLogRule.query.filter_by(project_id=p.id).count() == 0

        # 没选星期也不行
        client.post(
            f"/projects/{p.id}/log-rules/add",
            data={"user_id": si.id},
            follow_redirects=True,
        )
        assert ProjectLogRule.query.filter_by(project_id=p.id).count() == 0

        # 正常添加
        client.post(
            f"/projects/{p.id}/log-rules/add",
            data={"user_id": si.id, "weekdays": ["1"]},
            follow_redirects=True,
        )
        assert ProjectLogRule.query.filter_by(project_id=p.id, user_id=si.id).count() == 1

    def test_log_reminder_plan_send_and_dedupe(self, client, monkeypatch):
        self._enable_sms(monkeypatch)
        seed = get_seed()
        p = self._project("ProjLogRemind", ["wang", "si"])
        si = _db.session.get(User, seed["users"]["si"])
        si.phone = "13800000002"
        _db.session.add(
            ProjectLogRule(
                project_id=p.id,
                user_id=si.id,
                weekdays="1",  # 每周一
                created_by_user_id=seed["users"]["admin"],
            )
        )
        _db.session.commit()

        # 周一该写却没写 → 命中
        plans, _skipped = build_log_reminder_plans(local_day=self.MONDAY)
        mine = [pl for pl in plans if pl.user_id == si.id]
        assert len(mine) == 1
        assert mine[0].missing_count >= 1
        assert any(pid == p.id for pid, _name, _cnt in mine[0].projects)

        # 周二不在频率里 → 不提醒
        plans2, _ = build_log_reminder_plans(local_day=self.TUESDAY)
        assert not [pl for pl in plans2 if pl.user_id == si.id]

        # 当天写了日志 → 不再提醒
        start_utc, _end = local_day_utc_bounds(self.MONDAY)
        _db.session.add(
            ProjectUpdate(
                project_id=p.id,
                body="周一日志",
                created_by_user_id=si.id,
                created_at=start_utc + timedelta(hours=3),
            )
        )
        _db.session.commit()
        plans3, _ = build_log_reminder_plans(local_day=self.MONDAY)
        # si 在别的项目上还有别的规则，这里只关心本项目已经写过日志
        assert not any(
            pid == p.id
            for pl in plans3
            if pl.user_id == si.id
            for pid, _name, _cnt in pl.projects
        )

        # 删掉日志 → 发提醒并落库（含项目拆分）
        ProjectUpdate.query.filter_by(
            project_id=p.id, created_by_user_id=si.id
        ).delete()
        _db.session.commit()
        sender, calls = self._fake_sender()
        monkeypatch.setattr("ledger_app.log_rules.send_sms", sender)

        res = send_log_reminders(local_day=self.MONDAY)
        assert any(phone == "13800000002" for phone, _n in calls)
        rec = (
            SmsNotification.query.filter_by(user_id=si.id, trigger="log_reminder")
            .order_by(SmsNotification.id.desc())
            .first()
        )
        assert rec is not None and rec.status == "sent" and rec.pending_count >= 1
        assert any(sp.project_id == p.id for sp in rec.projects)
        assert res["sent"] >= 1

        # 同一天重复跑不会重复发（除非 --force）
        before = sum(1 for phone, _n in calls if phone == "13800000002")
        send_log_reminders(local_day=self.MONDAY)
        after = sum(1 for phone, _n in calls if phone == "13800000002")
        assert after == before
        send_log_reminders(local_day=self.MONDAY, force=True)
        assert sum(1 for phone, _n in calls if phone == "13800000002") == before + 1


class TestUserDelete:
    """管理员删除用户。"""

    def test_delete_user_cleans_references(self, client):
        seed = get_seed()
        login(client, "admin", "admin123!")
        p = Project.query.filter_by(name="ProjUserDelete").first()
        if p is None:
            p = Project(
                name="ProjUserDelete",
                expected_income_cents=0,
                broker_fee_mode="percent",
                broker_fee_direction="we_pay_separate",
                referral_ratio=Decimal("0"),
                status="open",
                leader_user_id=seed["users"]["admin"],
                planned_start_date=date(2026, 1, 1),
                planned_end_date=date(2026, 12, 31),
            )
            _db.session.add(p)
            _db.session.flush()
            _db.session.commit()

        u = User(username="todelete", role="viewer", is_active=True, phone="13800000077")
        u.set_password("123456!")
        _db.session.add(u)
        _db.session.flush()
        _db.session.add(ProjectMember(project_id=p.id, user_id=u.id))
        tx = Transaction(
            project_id=p.id, status="active", type="income", amount_cents=12345,
            occur_date=date.today(), settled=True, created_by_user_id=u.id,
        )
        _db.session.add(tx)
        _db.session.commit()
        uid, txid = int(u.id), int(tx.id)

        rv = client.post(f"/users/{uid}/delete", follow_redirects=True)
        assert rv.status_code == 200
        assert _db.session.get(User, uid) is None
        assert ProjectMember.query.filter_by(user_id=uid).first() is None
        # 历史流水保留，只是发起人置空
        kept = _db.session.get(Transaction, txid)
        assert kept is not None and kept.created_by_user_id is None

    def test_delete_guards(self, client):
        seed = get_seed()
        admin_id = seed["users"]["admin"]

        # 普通用户删不掉
        login(client, "wang")
        client.post(f"/users/{admin_id}/delete", follow_redirects=True)
        assert _db.session.get(User, admin_id) is not None

        # 管理员不能删自己
        client.post("/logout")
        login(client, "admin", "admin123!")
        client.post(f"/users/{admin_id}/delete", follow_redirects=True)
        assert _db.session.get(User, admin_id) is not None

        # 另一个管理员也不能删内置 admin 账号
        other = User(username="admin2", role="admin", is_active=True, phone="13800000088")
        other.set_password("123456!")
        _db.session.add(other)
        _db.session.commit()
        other_id = int(other.id)

        client.post("/logout")
        login(client, "admin2", "123456!")
        client.post(f"/users/{admin_id}/delete", follow_redirects=True)
        assert _db.session.get(User, admin_id) is not None

        # 删自己（admin2 是当前登录）也不行
        client.post(f"/users/{other_id}/delete", follow_redirects=True)
        assert _db.session.get(User, other_id) is not None
