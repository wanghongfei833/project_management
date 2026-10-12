#!/usr/bin/env python3
"""查短信投递回执：某号码某天发了哪些、投递成功还是失败（阿里云 QuerySendDetails）。

用法（服务器上执行）::

    cd /root/project/PM
    set -a; . ./.env.sms; set +a
    DATABASE_URL="sqlite:////root/project/PM/instance/ledger.db" UPLOAD_FOLDER=uploads SECRET_KEY=x \
      /root/miniconda3/envs/TIE/bin/python scripts/query_sms_status.py --phone 15007943612 --date 20261011
    # 不给 --date 时查最近 5 天
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="查询短信投递回执")
    parser.add_argument("--phone", required=True, help="手机号")
    parser.add_argument("--date", default=None, help="日期 yyyymmdd（默认查最近 5 天）")
    parser.add_argument("--days", type=int, default=5, help="不带 --date 时往前查几天")
    args = parser.parse_args()

    from run import create_app  # noqa: E402

    app = create_app()
    with app.app_context():
        from ledger_app.sms import query_send_details, sms_configured

        if not sms_configured():
            print("[skip] 短信未配置（缺 ALIYUN_SMS_* 环境变量）")
            return 0

        if args.date:
            days = [args.date]
        else:
            today = date.today()
            days = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(args.days + 1)]

        for day in days:
            res = query_send_details(args.phone, day)
            if not res["ok"]:
                print(f"[{day}] 查询失败：{res['code']} {res['message']}")
                continue
            if not res["records"]:
                print(f"[{day}] 无发送记录")
                continue
            print(f"[{day}] 共 {res['total']} 条：")
            for r in res["records"]:
                print(
                    "   %s  状态=%s  错误码=%s  回执时间=%s  模板=%s  内容=%s"
                    % (
                        r["send_date"],
                        r["status_text"],
                        r["err_code"] or "-",
                        r["receive_date"] or "-",
                        r["template_code"] or "-",
                        (r["content"] or "")[:36],
                    )
                )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
