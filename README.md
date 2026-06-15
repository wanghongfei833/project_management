# 项目经济账本 (Project Ledger)

一个基于 Flask 的内部项目收支管理系统，面向小型团队或合伙人，提供项目流水记账、中介费拆账、审批工作流、分红管理、凭证附件等核心功能。

## 功能特性

### 📊 仪表盘与报表
- 全局入账/出账统计、净额概览
- 项目回款进度可视化（进度条 + 百分比）
- 进行中/已终止项目分类展示
- 未回款 Top10 图表
- 按日期范围筛选的流水报表 + 支出对方 Pie 图

### 📁 项目管理
- 项目创建、编辑、成员管理（支持多成员）
- 项目状态流转：**开放中 → 已终止 → 分红结算**
- 计划周期与时间进度跟踪（逾期自动标红）
- 项目进展记录（支持图文附件）
- 操作审计日志（自动记录关键操作）

### 💰 流水记账
- 收入/支出流水登记（金额精确到分）
- **审批工作流**：新增/修改/删除流水均需审核通过
  - 规则：**全体非管理员成员同意** 或 **管理员同意** 即自动执行
  - 管理员拥有绝对权力，操作即时生效
- 每笔流水支持多个凭证附件
- 流水分页查看 + 逐笔账户余额

### 🧮 中介费拆账
支持两种中介费用模式：
- **百分比模式**：按合同金额比例计提
- **固定金额模式**：全额扣除固定介绍费（不分摊）

两种资金流向：
- **我方另付**：流水记客户全额，介绍费从我方净额扣除
- **中介先扣**：流水记我方净额（介绍费已由中介扣留）

### 💵 分红管理
- 项目终止后，基于最终利润进行分红
- **分红对象排除管理员**，仅限项目普通成员
- 表格化分红登记：姓名 → 金额 → 动态剩余
- **一键均分**：剩余分红自动等额分配（余数归最后一人）
- 双重校验：不超过现金结余 + 不超过分红池

### 🔄 项目生命周期
```
开放中 → 发起终止申请 → 审批通过 → 已终止(只读) → 分红结算
                                                      ↓
                                              可发起复活申请 → 恢复为开放中
```

### 👥 权限体系
| 角色 | 权限 |
|---|---|
| **管理员** | 所有操作权限（增删改查、审批、用户管理） |
| **普通用户** | 仅查看所在项目、创建项目、发起审批申请 |

## 技术栈

| 层 | 技术 |
|---|---|
| **后端** | Python 3.12 + Flask 3.0 |
| **ORM** | SQLAlchemy + SQLite |
| **前端** | Jinja2 + Bootstrap 5 (Bootswatch Lux) + Chart.js |
| **认证** | Flask-Login + Werkzeug 密码哈希 |
| **部署** | Gunicorn + systemd (Ubuntu) |
| **UI 增强** | Bootstrap Icons + Chart.js |

## 快速开始

### 环境要求
- Python 3.10+
- pip

### 安装与运行

```bash
# 克隆仓库
git clone https://github.com/wanghongfei833/project_management.git
cd project_management

# 创建虚拟环境（可选）
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\Activate.ps1  # Windows

# 安装依赖
pip install -r requirements.txt

# 启动开发服务器
python run.py
```

浏览器打开 `http://127.0.0.1:5000`。

### 初始管理员账号

首次启动自动创建管理员账号：

| 字段 | 值 |
|---|---|
| 用户名 | `admin` |
| 密码 | `admin123!` |

> ⚠️ **生产环境务必修改默认密码！**

## 项目结构

```
project_management/
├── run.py                 # 开发启动入口
├── requirements.txt       # Python 依赖
├── README.md              # 本文件
├── .gitignore
├── ledger_app/            # Flask 应用核心
│   ├── __init__.py        # 应用工厂
│   ├── routes.py          # 所有 HTTP 路由
│   ├── models.py          # ORM 数据模型
│   ├── forms.py           # WTForms 表单
│   ├── project_finance.py # 财务计算引擎
│   ├── schema.py          # 数据库迁移
│   ├── seed.py            # 种子数据
│   ├── utils.py           # 工具函数
│   ├── upload_paths.py    # 附件路径管理
│   ├── extensions.py      # SQLAlchemy / LoginManager 实例
│   └── middleware.py      # 子路径部署中间件
├── templates/             # Jinja2 模板
│   ├── base.html          # 布局模板
│   ├── dashboard.html     # 仪表盘
│   ├── login.html         # 登录页
│   ├── project_detail.html# 项目详情
│   ├── project_form.html  # 项目表单
│   ├── project_dividend.html # 分红页面
│   ├── transaction_form.html  # 流水表单
│   ├── transaction_edit.html  # 流水修改
│   ├── transactions_list.html # 流水列表
│   ├── reports.html       # 报表
│   ├── users_list.html    # 用户管理
│   └── ...                # 其余页面
├── scripts/               # 运维脚本
│   ├── start_pm_server.sh
│   └── stop_pm_server.sh
└── uploads/               # 附件存储（不纳入版本控制）
```

## 配置项（环境变量）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `SECRET_KEY` | Flask 密钥（生产务必设置强密钥） | `dev-secret-key-change-me` |
| `DATABASE_URL` | 数据库连接串 | `sqlite:///ledger.db` |
| `UPLOAD_FOLDER` | 附件上传目录 | `./uploads` |
| `URL_PREFIX` | 子路径部署前缀 | 空（如 `/PM`） |

## 金额单位

- **界面展示**：元（保留两位小数）
- **内部存储**：分（`BigInteger`，避免浮点误差）
- 转换：`元 × 100 = 分`，`分 ÷ 100 = 元`

## 部署（生产环境）

### Nginx + Gunicorn + systemd

参考 `scripts/` 目录下的启停脚本，结合 systemd 服务配置：

```ini
[Unit]
Description=Private PM (Flask via gunicorn)
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=/path/to/project
Environment="SECRET_KEY=你的强密钥"
Environment="DATABASE_URL=sqlite:////path/to/project/instance/ledger.db"
Environment="UPLOAD_FOLDER=/path/to/project/uploads"
Environment="URL_PREFIX=/PM"
ExecStart=/path/to/python -m gunicorn -w 2 -b 127.0.0.1:5002 run:app
Restart=always

[Install]
WantedBy=multi-user.target
```

## 安全说明

- 所有 POST 请求受 CSRF 保护
- 附件访问基于项目成员权限校验
- 上传路径做路径穿越防护
- 登录密码使用 Werkzeug 安全哈希
- 金额以分为单位存储，无浮点精度问题

## License

MIT
