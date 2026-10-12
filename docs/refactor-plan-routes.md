# `routes.py` 拆分：方案与执行结果

> **状态：已于 2026-10-12 执行完成**（`routes.py` 4461 行 → 最大模块 851 行）。
> 执行方式：用一个一次性脚本按函数名**机械搬运**，不改一行逻辑；
> 拆分前后对比 **URL 地图 64 条完全一致**、60 个测试全绿、18 个页面冒烟通过。

## 执行结果（实际结构）

```
ledger_app/
├── blueprints/                 # 全部路由仍注册在名为 main 的蓝图里（url_for('main.xxx') 不变）
│   ├── __init__.py             # bp = Blueprint("main", __name__) + import 各领域触发注册
│   ├── _common.py  (128 行)    # 原 routes.py 的 import 块 + 服务层 + 常量（各模块 from ._common import *）
│   ├── core.py     (154 行)    # 登录/登出/改密、待办页、健康检查、uploads、before/after_request
│   ├── dashboard.py(329 行)    # 仪表盘、报表
│   ├── users.py    (228 行)    # 用户 CRUD / 重置密码 / 删除
│   ├── sms_admin.py(254 行)    # 短信页、立即发送、同步投递回执、测试短信
│   ├── exports.py  (279 行)    # 全局搜索、CSV 导出、个人收入
│   ├── projects.py (848 行)    # 项目 CRUD、详情页、日志规则、进展日志
│   ├── transactions.py(700行)  # 流水 CRUD + 新增/修改/删除审批
│   ├── lifecycle.py(851 行)    # 终止/复活/删除项目/结束日期变更/追加应收
│   └── dividend_routes.py(165行) # 分红页与分红提交
├── services/                   # 跨模块共享逻辑（原 routes.py 里的辅助函数）
│   ├── access.py   (111 行)    # 权限与可见性
│   ├── approvals.py(198 行)    # 审批判定（全部同意 / 可否执行）
│   ├── projects.py (76 行)     # 应收合计、状态切换、操作日志
│   ├── money.py    (95 行)     # 分/元换算、CSV 响应、日期区间解析
│   └── attachments.py(165 行)  # 附件读写
└── routes.py       (40 行)     # 兼容层：只 re-export bp 与少量旧引用（如测试用的 _dividend_cash_balance_cents）
```

**后续可选的进一步拆分**：`lifecycle.py`（851 行）可按「项目终止/复活」与「删除/追加应收」再拆两份；
`projects.py`（848 行）里的 `project_detail()` 单函数就有 370 行，可单独成文件。不再拆分也不影响可维护性。

## 新增迁移版本表（已实现）

SQLite 没有内置迁移/回滚机制（也不必换 MySQL）。现在库里有一张：

```sql
CREATE TABLE schema_migrations (
  version VARCHAR(64) PRIMARY KEY,
  note VARCHAR(255),
  applied_at DATETIME NOT NULL
);
```

- 历史所有「建表/补列」逻辑被包成 **0001 历史兜底迁移**，每步自带「存在就跳过」判断；
  老库第一次启动时会全部命中「已存在」，然后登记为已应用（不会改动任何数据）。
- 以后加迁移：在 `ledger_app/schema.py` 的 `MIGRATIONS` 列表**追加**一条 `("0002", "说明", 函数)`，启动时自动只跑没记录过的。
- 查看状态：`python scripts/schema_status.py`（打印已应用/未应用，并把未应用的补跑）。
- 回滚：表里记了版本，出问题能定位是哪一段；真要 down 语句，在迁移函数旁边写一个对应函数手工执行即可。

---

## 以下是当初的方案（执行时基本按此进行）

> 现状：`ledger_app/routes.py` 约 3900 行、80+ 个路由，财务/审批/提醒/用户全在一个文件里。
> 改一处容易误伤别处（比如上次「未收到费用」的口径 bug，就是同一公式散落 4 处导致的）。
> 本文件是**方案**，不动手；你确认后下阶段执行。

## 一、目标

1. 每个领域一个 blueprint 文件，单文件控制在 300~500 行；
2. 共享逻辑（权限、金额汇总、分红口径）只保留一份实现；
3. **行为零变化**：拆完所有路由的 URL、参数、权限、返回完全一致，测试全绿。

## 二、目标结构

```
ledger_app/
├── blueprints/
│   ├── __init__.py          # register_blueprints(app, csrf, login_manager)
│   ├── auth.py              # 登录/登出/改密/健康检查
│   ├── users.py             # 用户 CRUD、重置密码、删除、手机号
│   ├── projects.py          # 项目 CRUD、详情页、日志规则、进展日志、日志附件
│   ├── transactions.py      # 流水 CRUD + 新增/修改/删除审批
│   ├── lifecycle.py         # 终止 / 复活 / 删除项目 / 结束日期变更 / 追加应收
│   ├── dividends.py         # 分红页与分红提交
│   ├── approvals.py         # 待我处理页、登录弹窗数据（before_request）
│   ├── reports.py           # 报表、个人收入、搜索、CSV 导出
│   └── sms_admin.py         # 短信页、测试发送、同步投递回执
├── services/
│   ├── access.py            # is_admin / 可见项目 / 成员判断（现 routes 里的 _accessible_*）
│   ├── approvals_svc.py     # 审批判定（fully_approved / can_execute 一族）
│   ├── projects_svc.py      # project_expected_total_cents、父子项目聚合
│   └── transactions_svc.py  # 金额汇总（统一 status/settled/is_void 过滤）
├── dividends.py             # ✅ 已完成：分红口径唯一实现
├── project_finance.py       # ✅ 保持：财务口径引擎（纯函数）
├── pending.py               # ✅ 保持：待办聚合
├── sms.py / sms_alerts.py / sms_delivery.py / log_rules.py   # ✅ 保持
└── routes.py                # 拆完删除（或只留兼容 import）
```

## 三、执行顺序（每步都要跑测试 + 部署验证）

| 步骤 | 内容 | 风险 | 验证 |
|---|---|---|---|
| 1 | 抽出 `services/transactions_svc.py`：把 20+ 处金额汇总收敛成带统一过滤的helper | 低 | 现有 55 个测试 + 线上报表/仪表盘数字比对 |
| 2 | 抽出 `services/access.py`、`services/approvals_svc.py`（纯搬运，不改逻辑） | 低 | 测试 + 抽查普通用户视角 |
| 3 | 拆 `blueprints/reports.py`（报表/收入/搜索/导出，最独立的一块） | 低 | 页面 200 + 导出 CSV 内容比对 |
| 4 | 拆 `blueprints/users.py`、`blueprints/sms_admin.py` | 低 | 用户页/短信页回归 |
| 5 | 拆 `blueprints/transactions.py`（含 4 类审批） | 中 | 用测试覆盖审批全流程 + 线上真实待办走一遍 |
| 6 | 拆 `blueprints/lifecycle.py`、`dividends.py` | 中 | 已终止项目页/分红页回归 |
| 7 | 拆 `projects.py`（最大的一块，最后做） | 中高 | 项目详情页逐块对比截图 |
| 8 | 删除 `routes.py`，更新 `docs/aliyun-ops.md` | 低 | 全量测试 + 线上健康检查 |

原则：**一步一提交**，每步都可独立回滚；不在拆分的同时改逻辑（逻辑改动单独提）。

## 四、冻结的接口（拆分时必须保持不变）

- URL 与 endpoint 名（模板里到处在用 `url_for('main.xxx')`）：保留 `main` 这个 blueprint 名，或用 `app.add_url_rule` 兼容；
- `pending.py` / `log_rules.py` / `sms_alerts.py` 里的函数签名（定时任务脚本依赖）；
- 各审批路由的判定口径（非管理员成员全部同意 / 任一管理员同意）。

> endpoint 名保持 `main.xxx` 最省事：所有 blueprint 都用 `Blueprint("main", ...)` 会重名，
> 所以更稳的做法是**保留一个名为 `main` 的父 blueprint**，各领域用子 blueprint 注册进去，
> 或统一用 `Blueprint("main", __name__)` + 分文件定义后 `app.register_blueprint(part)` 时用
> `name="main"`。这一步在动手前先做个小验证。

## 五、收益

- 定位问题快：财务口径只在一个 service 里，不会再出现"4 处公式各写一遍"；
- 改动安全：改用户相关不会碰到流水相关；
- 新人可读：按业务分文件，而不是按"谁先写的"堆在一起。

## 六、顺带可选（同批或之后）

- **迁移版本表**：SQLite 本身没有内置的迁移/回滚机制（换 MySQL 没必要，也吃资源）。
  可以在库里加一张 `schema_migrations(version TEXT PRIMARY KEY, applied_at DATETIME)`，
  每段迁移写成编号函数，启动时只跑没记录过的；这样有版本可查、出错能定位。
  现在的 `ensure_sqlite_schema()` 是"看列在不在"，能跑，但没有版本与回滚能力。
