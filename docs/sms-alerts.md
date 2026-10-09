# 待办超时短信提醒

## 规则

- 某个申请在**别人那里**积压超过 **6 小时**未处理，就给「还没同意的人」发短信；
- 只要还没处理完，之后**每 6 小时再发一条**（用发送记录做冷却，不会重复轰炸）；
- 短信按人聚合：一次给一个人发一条，正文里的 `${number}` 是该人当前待处理条数；
- 申请人自己发起时会自动记一条同意，所以不会收到自己申请的提醒；
- 管理员有待办也会收到（管理员一票即生效）。

模板 `SMS_512630691`：

```
项目账本还有${number}条内容未处理，请及时登录仪表盘 - 项目账本进行处理。
```

## 谁会被通知

只有**填了手机号且账号启用**的人才会收到短信。手机号在「用户管理」里维护：

- 新增用户时必须填手机号；
- 老用户（包括 admin）在编辑页补录，列表页会显示「未填写」提醒。

## 服务端配置

1. 填密钥（RAM 账号需要 `dysms:SendSms` 权限）：

```bash
cd /root/project/PM
cp deploy/sms.env.example .env.sms
vi .env.sms            # 填 ACCESS_KEY_ID / ACCESS_KEY_SECRET / SIGN_NAME
chmod 600 .env.sms
```

2. 安装定时任务（每 30 分钟检查一次，满足条件才发）：

> 另外要让 **Web 服务**也能读到这个密钥文件（否则「短信」页的「发送测试短信」「立即检查并发送」会提示未配置）：
> 在 `/etc/systemd/system/private-pm.service` 里加一行
> `EnvironmentFile=-/root/project/PM/.env.sms`（带 `-` 表示文件不存在也能启动），
> 然后 `systemctl daemon-reload && systemctl restart private-pm`。服务器上已配置好。

3. 安装定时任务（每 30 分钟检查一次，满足条件才发）：

```bash
cp deploy/pm-sms-alert.service /etc/systemd/system/
cp deploy/pm-sms-alert.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pm-sms-alert.timer
systemctl list-timers pm-sms-alert.timer
```

4. 验证：

```bash
# 只看会给谁发，不实际发送
/root/miniconda3/envs/TIE/bin/python scripts/send_pending_sms.py --dry-run

# 发一条测试短信
/root/miniconda3/envs/TIE/bin/python scripts/send_pending_sms.py --test-phone 13800000000 --test-number 2

# 立刻跑一轮（忽略 6 小时冷却）
/root/miniconda3/envs/TIE/bin/python scripts/send_pending_sms.py --force

# 看定时任务日志
journalctl -u pm-sms-alert.service -n 50 --no-pager
```

## 页面

管理员导航栏「短信」页（`/PM/admin/sms`）：

- 配置状态（是否已配置密钥/签名/模板）；
- 「立即检查并发送」「强制发送一条」「发送测试短信」；
- **按项目统计**：每个项目发过多少条短信、涉及多少人；
- 选定项目后可见**发给了谁、各多少条**；
- 全部发送明细（时间/用户/号码/条数/状态/触发方式/失败原因），支持按人、项目、状态、时间筛选。

> 页面上的时间是北京时间（库里存的是 UTC，展示时 +8）。

## 排查

| 现象 | 原因 |
|---|---|
| 页面显示「短信未配置」 | `.env.sms` 没填或服务没重启 |
| 发送失败 `isv.SMS_SIGNATURE_ILLEGAL` | 签名名称与阿里云控制台不一致/未审核 |
| 发送失败 `isv.SMS_TEMPLATE_ILLEGAL` | 模板 CODE 不对或未审核 |
| 发送失败 `isv.BUSINESS_LIMIT_CONTROL` | 触发阿里云流控（同号码/同签名频率限制） |
| 一直不发 | 待办未满 6 小时、或 6 小时内已发过、或该用户没填手机号（页面会给出各原因的跳过人数） |

## 日志撰写提醒（每天 20:00）

### 先只给一个人试（测试白名单）

在 `/root/project/PM/.env.sms` 里加一行：

```
ALIYUN_SMS_ALLOW_PHONES=15682527196
```

然后 `systemctl restart private-pm`（定时任务用的是同一个文件，不用另外操作）。效果：

- **自动提醒**（6 小时待办提醒、每天 20:00 日志提醒）只发给白名单里的号码，其他人一律跳过（页面会显示跳过原因「不在测试白名单 N 人」）；
- 「短信」页会出现蓝色**测试模式**横幅，标出当前只发给谁；
- 「发送测试短信」按钮**不受白名单限制**（那是管理员手动指定号码的测试）。

确认没问题后，把这一行删掉（或留空）再重启，就全量放开了。

### 规则怎么配

项目详情页 →「📝 日志撰写规则」→ 选撰写人 + 勾选星期 → 添加。

- 周一到周日可多选，**七个都勾＝每天**；
- 同一个人重复添加＝修改频率，不会产生重复规则；
- 只有**管理员**和**项目负责人**能维护；撰写人必须是该项目成员；
- 项目终止后不再提醒。

### 判定与发送

- 每天晚上 20:00 检查：今天该写日志的人，如果**当天没有为该项目的日志**（`project_updates`），就发一条短信；
- 短信内容暂用待办模板，`${number}` = 当天未写的日志条数（一个人多个项目未写会合并成一条）；
- 同一天只发一次（按「业务日期」去重，重跑不会重复发）；
- 发送记录出现在「短信」页，触发方式显示为**日志提醒**。

### 网页提醒（和待办一样的弹窗）

登录后第一次打开页面会弹窗，里面分两块：**待审批（N）** 和 **今天要写的日志（M）**，日志那条后面
直接有「去写日志」按钮；点完（当天写过了）下次登录就不再出现。另外：

- 顶部导航「待办」铃铛：红色徽标＝待审批数，黄色徽标＝今天待写日志数；
- 「待办」页（`/PM/approvals`）下方有「今天要写的日志」区块；
- 仪表盘顶部也会出现一条黄色提示条。

### 安装 / 验证

```bash
cp deploy/pm-log-reminder.service /etc/systemd/system/
cp deploy/pm-log-reminder.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pm-log-reminder.timer
systemctl list-timers pm-log-reminder.timer

# 只看今天会提醒谁，不发送
/root/miniconda3/envs/TIE/bin/python scripts/send_log_reminders.py --dry-run
# 指定日期检查（排查用）
/root/miniconda3/envs/TIE/bin/python scripts/send_log_reminders.py --dry-run --date 2026-10-09
# 手动发一轮（忽略「今天已提醒」）
/root/miniconda3/envs/TIE/bin/python scripts/send_log_reminders.py --force
```

> 时间口径：库里存 UTC，业务按北京时间判断「今天」。
