#!/usr/bin/env python3
"""短信通道自检：给指定号码发一条测试短信，等回执并给出结论。

典型用途：阿里云后台「移动通道报备中」，报备完成后用它确认移动号是否已经能收到。

用法（服务器）::

    cd /root/project/PM
    set -a; . ./.env.sms; set +a
    export DATABASE_URL="sqlite:////root/project/PM/instance/ledger.db" UPLOAD_FOLDER=uploads SECRET_KEY=x
    # 发一条并等回执（默认最多等 5 分钟）
    /root/miniconda3/envs/TIE/bin/python scripts/check_sms_channel.py --phone 15114067585
    # 只查最近一条回执，不再发送
    /root/miniconda3/envs/TIE/bin/python scripts/check_sms_channel.py --phone 15114067585 --no-send
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TZ_OFFSET_HOURS = 8


def main() -> int:
    parser = argparse.ArgumentParser(description="短信通道自检")
    parser.add_argument("--phone", required=True, help="测试号码（例如一个移动号）")
    parser.add_argument("--wait", type=int, default=300, help="等回执的最长秒数，默认 300")
    parser.add_argument("--no-send", action="store_true", help="只查最近回执，不发送")
    args = parser.parse_args()

    from run import create_app

    app = create_app()
    with app.app_context():
        from ledger_app.sms import (
            error_hint,
            normalize_phone,
            query_send_details,
            sms_configured,
        )

        phone = normalize_phone(args.phone)
        if not sms_configured():
            print("[skip] 短信未配置（缺 ALIYUN_SMS_* 环境变量）")
            return 0

        if not args.no_send:
            from ledger_app.sms_alerts import send_test_sms

            res = send_test_sms(phone, 1, hours=1)
            if not res.get("ok"):
                print("[fail] 发送被拒：%s %s（%s）" % (res.get("code"), res.get("message"), error_hint(res.get("code"))))
                return 1
            print("[sent] 已受理，BizId=%s，等回执…（最多 %d 秒）" % (res.get("biz_id"), args.wait))

        deadline = time.time() + (0 if args.no_send else args.wait)
        last = None
        while True:
            day = (datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=TZ_OFFSET_HOURS)).strftime("%Y%m%d")
            r = query_send_details(phone, day)
            records = [x for x in r["records"] if x["phone"] == phone]
            if records:
                last = records[-1]
                if last["status_text"] in ("成功", "失败"):
                    break
            if time.time() >= deadline:
                break
            time.sleep(20)

        if not last:
            print("[?] 还没有查到该号码今天的发送记录")
            return 1

        print("结果：%s  错误码=%s  回执时间=%s" % (last["status_text"], last["err_code"] or "-", last["receive_date"] or "-"))
        if last["status_text"] == "成功":
            print("✅ 通道已打通：该号码可以正常收到短信。")
            return 0
        hint = error_hint(last["err_code"])
        print("❌ 仍未打通：%s%s" % (last["err_code"] or "", ("（" + hint + "）") if hint else ""))
        if last["err_code"] == "PORT_NOT_REGISTERED":
            print("   → 多为该号段的运营商通道未开通/仍在报备，去阿里云短信控制台看「签名报备状态」。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
