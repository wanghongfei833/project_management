#!/usr/bin/env python3
"""日志撰写提醒（每天 20:00 由 systemd timer 调用）。

规则见 docs/sms-alerts.md：项目里给某人配了「每周一/三/五写日志」，当天该写却没写，
晚上 20:00 就给他发一条短信（暂用待办事项模板，后续可换模板）。

用法::

    python scripts/send_log_reminders.py                  # 正常一轮（当天只发一次）
    python scripts/send_log_reminders.py --dry-run        # 只看会提醒谁
    python scripts/send_log_reminders.py --force          # 忽略「今天已发过」
    python scripts/send_log_reminders.py --date 2026-10-09
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="日志撰写提醒")
    parser.add_argument("--dry-run", action="store_true", help="只列出会提醒谁，不发送")
    parser.add_argument("--force", action="store_true", help="忽略「今天已经提醒过」")
    parser.add_argument("--date", default=None, help="按指定日期检查（默认今天）")
    args = parser.parse_args()

    day = None
    if args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").date()

    from run import create_app  # noqa: E402

    app = create_app()
    with app.app_context():
        from ledger_app.log_rules import send_log_reminders
        from ledger_app.sms import sms_configured

        if not sms_configured() and not args.dry_run:
            print("[skip] 短信未配置：需要 ALIYUN_SMS_ACCESS_KEY_ID / ACCESS_KEY_SECRET / SIGN_NAME")
            return 0

        summary = send_log_reminders(local_day=day, dry_run=args.dry_run, force=args.force)
        print(f"[date] {summary['date']}")
        for plan in summary["plans"]:
            detail = "、".join(
                f"{name}({cnt})" for _pid, name, cnt in plan.projects
            )
            print(f"  -> {plan.username} {plan.phone}：{plan.missing_count} 条日志未写 {detail}")
        for msg in summary["messages"]:
            print(f"[info] {msg}")
        sk = summary["skipped"]
        print(
            "[summary] 命中 {plans} 人，成功 {sent} 条，失败 {failed} 条；"
            "跳过：没手机号 {no_phone}、号码不合法 {invalid_phone}、不在测试白名单 {not_allowed}、"
            "今天已提醒 {already_reminded}".format(
                plans=len(summary["plans"]), sent=summary["sent"], failed=summary["failed"], **sk
            )
        )
        return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
