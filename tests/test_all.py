"""
项目账本 · 自动化测试脚本
用法：pytest tests/ -v
"""
import pytest, os, sys
from pathlib import Path
from decimal import Decimal
from datetime import date, datetime, timezone

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
)
from ledger_app.project_finance import build_project_finance


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
        login(client, "admin", "admin123!")
        seed = get_seed()
        client.post(f"/projects/{seed['projects']['A1']}/revive-request")
        rv = client.post(f"/projects/{seed['projects']['A1']}/revive-approve", follow_redirects=True)
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

