# 阿里云服务器连接与运维（项目管理 / 项目账本）

## 0. 速查表

| 项 | 值 |
|---|---|
| 服务器 | `39.108.114.245`（阿里云 ECS，Ubuntu 22.04.5 LTS，40G 盘） |
| 连接方式 | `ssh aliyun`（本机 `~/.ssh/config` 已配好，用私钥登录） |
| 线上网址 | https://www.xingtongkeji.cn/PM （80 端口自动跳 https） |
| 项目目录 | `/root/project/PM` |
| 数据库 | `/root/project/PM/instance/ledger.db`（SQLite） |
| 附件目录 | `/root/project/PM/uploads` |
| 服务 | `private-pm.service`（gunicorn，2 worker，监听 `127.0.0.1:5002`） |
| Python | `/root/miniconda3/envs/TIE/bin/python`（3.12.0） |
| 健康检查 | `/PM/health` |
| 定时任务 | `pm-sms-alert.timer`（每 30 分钟）、`pm-log-reminder.timer`（每天 20:00） |
| 短信密钥 | `/root/project/PM/.env.sms`（权限 600，不入库） |
| 备份 | 代码 `/root/project/PM_backup_*.tar.gz`、数据库 `/root/backups/ledger-*.db` |

## 1. 从本机怎么连

本机（Windows）已经配好，可直接用：

```powershell
ssh aliyun                                             # 登录服务器
ssh aliyun "systemctl is-active private-pm.service"    # 只执行一条命令
scp .\run.py aliyun:/root/project/PM/run.py            # 传单个文件
```

配置在 `C:\Users\10095\.ssh\config`：

```
Host aliyun
    HostName 39.108.114.245
    User root
    Port 22
    IdentityFile C:\Users\10095\.ssh\Aliyun.pem
    IdentitiesOnly yes
```

私钥 `C:\Users\10095\.ssh\Aliyun.pem`（另有一份 `aliyun_key.pem` 备份）。**换电脑只要带这两样**，
或者把公钥加到服务器 `/root/.ssh/authorized_keys` 即可。

常见问题：

| 报错 | 处理 |
|---|---|
| `Permission denied (publickey)` | 私钥路径写错，或文件权限过宽（把继承权限关掉，只留本人可读） |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | 服务器重装过：`ssh-keygen -R 39.108.114.245` 再连 |
| 首次连接卡在 yes/no | 加 `-o StrictHostKeyChecking=accept-new` |
| 连接超时 | 检查阿里云安全组的 22 端口是否放行、本机网络 |

## 2. 服务器上有什么（别动不该动的）

请求链路：

```
浏览器 https://www.xingtongkeji.cn/PM/...
   └─ nginx (:443, /etc/nginx/sites-enabled/default)
        location /PM/ → proxy_pass http://127.0.0.1:5002
                        并设置 X-Forwarded-Prefix: /PM（前缀中间件靠它工作）
        └─ gunicorn（private-pm.service，2 worker）
             └─ Flask 应用（URL_PREFIX=/PM，模板里的 url_for 才带前缀）
```

同机还跑着这些服务，**改 PM 的时候不要碰它们**：

| 服务 | 位置 | 说明 |
|---|---|---|
| `xingtong-auth` / `xingtong-date-reminder` / `xingtong-gongdi` / `xingtong-web-portial` | `/opt/xingtong`、`/var/www/XingTong` | 小程序后端（短信密钥的来源也在 date-reminder 的 config.yaml 里） |
| `smart-agri`、`face-app` | — | 其它业务 |
| 禅道 `zbox`、考勤 `attendance_mvp`、`frps` | `/opt/zbox`、`/opt/attendance_mvp_data` | 与账本无关 |

## 3. 日常操作（最常用）

```bash
# 服务状态 / 健康检查
systemctl status private-pm --no-pager
curl -sk -o /dev/null -w '%{http_code}\n' -H 'Host: www.xingtongkeji.cn' https://127.0.0.1/PM/health   # 期望 200

# 看日志（排错第一站）
journalctl -u private-pm -n 100 --no-pager
journalctl -u private-pm -f

# 重启
systemctl restart private-pm

# 定时任务
systemctl list-timers pm-sms-alert.timer pm-log-reminder.timer --no-pager
journalctl -u pm-sms-alert -n 50 --no-pager
journalctl -u pm-log-reminder -n 50 --no-pager

# 手动跑一轮提醒（dry-run 不发送、不写记录，只看会给谁发）
cd /root/project/PM
set -a; . ./.env.sms; set +a
export DATABASE_URL="sqlite:////root/project/PM/instance/ledger.db"
export UPLOAD_FOLDER=/root/project/PM/uploads SECRET_KEY=probe URL_PREFIX=/PM
/root/miniconda3/envs/TIE/bin/python scripts/send_pending_sms.py --dry-run
/root/miniconda3/envs/TIE/bin/python scripts/send_log_reminders.py --dry-run
```

## 4. 更新代码（注意：服务器**不是** git 仓库）

服务器上的代码是「文件拷贝」式部署，没有 `.git`，所以**不能**在服务器上 `git pull`。
标准流程：

```powershell
# 1) 本机：改完先跑测试，再提交
F:\Anaconda3\envs\aliyun\python.exe -m pytest tests -q
git add -A; git commit -m "..."
# 2) 推送仓库（本机没开代理时看文末「Git 推送」一节）
git -c http.proxy= -c http.sslBackend=schannel -c http.curloptResolve=github.com:443:20.27.177.113 push origin master
# 3) 只把改动的文件传上去
scp ledger_app\routes.py aliyun:/root/project/PM/ledger_app/routes.py
scp templates\base.html  aliyun:/root/project/PM/templates/base.html
```

```bash
# 4) 服务器：语法检查 → 重启 → 验证
ssh aliyun "cd /root/project/PM && /root/miniconda3/envs/TIE/bin/python -m py_compile ledger_app/*.py && systemctl restart private-pm && sleep 4 && systemctl is-active private-pm && curl -sk -o /dev/null -w 'health: %{http_code}\n' -H 'Host: www.xingtongkeji.cn' https://127.0.0.1/PM/health"
```

铁律：

1. **不要覆盖 `instance/` 和 `uploads/`**——那是线上数据；
2. 动代码前先备份：
   ```powershell
   # PowerShell 里用单引号，$(date ...) 才会交给服务器上的 bash 执行
   ssh aliyun 'cd /root/project && tar -czf PM_backup_$(date +%Y%m%d-%H%M%S).tar.gz --exclude=./PM/instance --exclude=./PM/uploads PM'
   ```
3. 表结构变化不用手工迁移：启动时 `ledger_app/schema.py` 会自动补列/建表（并发建表已做重试，不会再把 worker 搞崩）。

## 5. 数据同步（线上 → 本地）

一条命令搞定：服务器端用 SQLite 热备份生成一致快照 → 下载 → 逐项校验 → 备份本地 → 覆盖。

```powershell
powershell -ExecutionPolicy Bypass -File scripts\pull_aliyun_data.ps1 -Mode overwrite -PythonExe 'F:\Anaconda3\envs\aliyun\python.exe'
```

- 默认 `-Mode isolated`：只下载到 `_aliyun_snapshot\` 供比对，**不动本地**；
- `-Mode overwrite`：覆盖本地 `instance\ledger.db` 与 `uploads\`，旧数据自动备份到 `_local_backup\<时间戳>\`；
- 校验不通过会**中止**（例如附件缺失、孤儿文件），按提示排查后重跑；
- 反向（本地 → 线上）没有脚本，务必手工、逐个文件确认。

## 6. 短信息（发送与配置）

- 配置文件：`/root/project/PM/.env.sms`（`chmod 600`，**不在 git 里**），变量见 `deploy/sms.env.example`；
- 密钥来源：复用小程序 `date-reminder` 服务的 AccessKey（`/opt/xingtong/services/date-reminder/config.yaml`），
  **轮换密钥时两处都要改**；
- 读取方式：Web 服务与两个定时任务都通过 `EnvironmentFile=-/root/project/PM/.env.sms` 读取，
  改完执行 `systemctl restart private-pm` 即可（定时任务每次启动时重新读，自动生效）；
- 验证：登录「短信」页（`/PM/admin/sms`）点「发送测试短信」，或命令行
  `... send_pending_sms.py --test-phone 138xxxxxxxx --test-number 2`；
- 想先只发给个别人试：在 `.env.sms` 里加 `ALIYUN_SMS_ALLOW_PHONES=156...,150...`（逗号分隔），页面会出现「测试模式」横幅；清空即全量放开。

## 7. 直接查数据库

```bash
/root/miniconda3/envs/TIE/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('/root/project/PM/instance/ledger.db')
for row in c.execute("select id, name, status from projects order by id"):
    print(row)
PY
```

写操作前**必须先备份**（见 §8）。gunicorn 同时在写时偶发 `database is locked`，重试即可。

## 8. 备份都在哪

| 内容 | 位置 | 说明 |
|---|---|---|
| 代码 | `/root/project/PM_backup_<时间戳>.tar.gz` | 含 env 之外的整个项目（排除 instance/uploads） |
| 数据库 | `/root/backups/ledger-<时间戳>.db` | 用 sqlite3 backup API 热备，可直接替换回去 |
| 其它 | `/root/backups/orphans-*`、`/root/backups/private-pm.service.*` | 清理出的孤儿附件、改过的 systemd unit 备份 |

## 9. 出问题怎么查

| 现象 | 先看什么 | 常见原因 |
|---|---|---|
| 页面 502 | `systemctl status private-pm` + `journalctl -u private-pm -n 50` | worker 起不来、数据库锁、代码语法错误 |
| 启动报 `table ... already exists` | 同上 | 多 worker 并发建表（已有重试逻辑；若复现看 `ledger_app/__init__.py::_bootstrap_schema`） |
| 页面样式/图标缺失 | 浏览器控制台 | 主题走 CDN（jsdelivr），本机/办公网访问不了 CDN |
| 短信没发出去 | `/PM/admin/sms` 页面 | 密钥未配置、不在白名单、没填手机号、未满 6 小时、6 小时内已发过——页面会分别给出跳过人数 |
| 数据校验报错 | `_aliyun_snapshot\` | 附件记录与磁盘文件不一致，用 `scripts/prune_orphan_uploads.py --apply` 归档孤儿文件 |

## 10. 阿里云控制台相关（需要登录阿里云时）

| 要做的事 | 去哪 |
|---|---|
| 重启/查 ECS、看磁盘、安全组 | 控制台 → 云服务器 ECS（实例 `39.108.114.245`，安全组需放行 22/80/443） |
| 短信签名、模板审核 | 控制台 → 短信服务 → 国内消息 → 签名管理 / 模板管理（当前模板 `SMS_512630691`） |
| AccessKey 管理与轮换 | 控制台右上角头像 → AccessKey 管理（建议用 RAM 子账号，只给 `dysms:SendSms`） |
| 域名解析 | 控制台 → 云解析 DNS（`xingtongkeji.cn` → `www` 指向本机） |

## 11. Git 推送（本机没开 VPN 时）

本机 git 全局配置了代理 `http://127.0.0.1:7890`，且系统级 gitconfig 里 `http.sslBackend=openssl`
（这版 Git for Windows 只支持 `schannel`）。没开代理时用命令行**临时绕过**，**不要改全局配置**：

```powershell
# 先找一个能连通的 GitHub IP（返回 200 即可用）
curl.exe -sS --max-time 8 --resolve github.com:443:20.27.177.113 -o NUL -w "%{http_code}`n" https://github.com

# 用固定 IP 推送
git -c http.proxy= `
    -c http.sslBackend=schannel `
    -c http.curloptResolve=github.com:443:20.27.177.113 `
    push origin master
```

说明：可用 IP 会随时间变化（`20.27.177.113`、`140.82.113.4` 曾可用；`20.205.243.166` 在本网段不通），
换 IP 前先用上面的 curl 探测。
