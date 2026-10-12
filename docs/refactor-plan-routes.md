# 规划：`routes.py` 拆分（下阶段执行）

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
