#!/usr/bin/env python3
"""待办超时短信提醒（给 systemd timer / cron 调用）。

规则：某个申请在他人那里积压超过 6 小时就给待处理人发短信，之后每 6 小时再发一条
（靠发送记录做冷却）。按人聚合，正文里的 ${number} 是该人当前待办条数。

用法（在项目根目录，或任意目录，用绝对路径调用）::

    python scripts/send_pending_sms.py                 # 正常一轮
    python scripts/send_pending_sms.py --dry-run       # 只列出会发给谁，不发送、不写记录
    python scripts/send_pending_sms.py --force         # 忽略 6 小时冷却，立即发
    python scripts/send_pending_sms.py --user-id 3     # 只检查某个用户
    python scripts/send_pending_sms.py --test-phone 13800000000 --test-number 2

依赖环境变量（与 private-pm.service 一致）：
DATABASE_URL / UPLOAD_FOLDER / SECRET_KEY，以及
ALIYUN_SMS_ACCESS_KEY_ID / ALIYUN_SMS_ACCESS_KEY_SECRET / ALIYUN_SMS_SIGN_NAME。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="待办超时短信提醒")
    parser.add_argument("--dry-run", action="store_true", help="只列出会发给谁，不实际发送")
    parser.add_argument("--force", action="store_true", help="忽略 6 小时冷却")
    parser.add_argument("--user-id", type=int, default=None, help="只检查指定用户")
    parser.add_argument("--test-phone", default=None, help="给该号码发一条测试短信")
    parser.add_argument("--test-number", type=int, default=1, help="测试短信里的条数")
    parser.add_argument("--test-hours", type=int, default=1, help="测试短信里的「已过去小时数」")
    parser.add_argument(
        "--trigger",
        default="auto",
        choices=["auto", "manual"],
        help="记录里的触发方式（默认 auto）",
    )
    args = parser.parse_args()

    from run import create_app  # noqa: E402  (需先设置好 sys.path)

    app = create_app()
    with app.app_context():
        from ledger_app.sms import sms_configured, sms_config
        from ledger_app.sms_alerts import send_pending_alerts, send_test_sms

        if args.test_phone:
            result = send_test_sms(
                args.test_phone, args.test_number, hours=args.test_hours
            )
            if result["ok"]:
                print(f"[ok] 测试短信已发送至 {result['phone']}，BizId={result['biz_id']}")
                return 0
            print(f"[fail] {result['code']}: {result['message']}")
            return 1

        cfg = sms_config()
        if not sms_configured(cfg) and not args.dry_run:
            print(
                "[skip] 短信未配置：需要 ALIYUN_SMS_ACCESS_KEY_ID / "
                "ALIYUN_SMS_ACCESS_KEY_SECRET / ALIYUN_SMS_SIGN_NAME"
            )
            return 0

        summary = send_pending_alerts(
            dry_run=args.dry_run,
            force=args.force,
            user_id=args.user_id,
            trigger=args.trigger,
        )

        for plan in summary["plans"]:
            detail = "、".join(f"{name}({count})" for _pid, name, count in plan.projects)
            print(
                f"  -> {plan.username} {plan.phone}：{plan.pending_count} 条待办 {detail}"
            )
        for msg in summary["messages"]:
            print(f"[info] {msg}")

        skipped = summary["skipped"]
        print(
            "[summary] 命中 {plans} 人，成功 {sent} 条，失败 {failed} 条；"
            "跳过：未填手机号 {no_phone}、号码不合法 {invalid_phone}、不在测试白名单 {not_allowed}、"
            "无待办 {no_pending}、今天已提醒过 {already_reminded}".format(
                plans=len(summary["plans"]), **skipped, **{
                    "sent": summary["sent"],
                    "failed": summary["failed"],
                }
            )
        )
        # 顺带同步最近两天的投递回执，这样「短信」页的「投递」列不用手动点也是最新的
        if not args.dry_run:
            try:
                from ledger_app.sms_delivery import sync_delivery_status

                res = sync_delivery_status(days=2)
                print(
                    "[info] 投递回执同步：更新 %d 条（送达 %d / 未送达 %d）"
                    % (res["updated"], res["delivered"], res["failed"])
                )
            except Exception as exc:  # 同步失败不影响发送
                print("[warn] 投递回执同步失败：%s: %s" % (type(exc).__name__, exc))
        return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
