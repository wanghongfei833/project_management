#!/usr/bin/env python3
"""服务健康检查 + 短信告警（给 systemd timer 每 5 分钟调用一次）。

逻辑：
- 请求本地 ``/health``；连续失败达到阈值 → 给 `PM_ALERT_PHONES` 发短信（1 小时最多一条）；
- 恢复后再发一条「服务已恢复」。

环境变量：
- ``PM_HEALTH_URL``    默认 ``http://127.0.0.1:5002/health``
- ``PM_ALERT_PHONES``  告警接收号码（逗号分隔）；不填则只写日志不发短信
- ``PM_ALERT_FAILS``   连续失败几次才告警，默认 3
- 短信密钥沿用 ``ALIYUN_SMS_*``（.env.sms）

退出码：0 正常；1 本次检查失败（便于 journal 里 grep）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "instance" / ".health_state.json"

DEFAULT_URL = "http://127.0.0.1:5002/health"


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # pragma: no cover
        print(f"[warn] 状态写入失败: {exc}")


def _probe(url: str, timeout: int = 10) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            ok = resp.status == 200 and '"ok"' in body.replace(" ", "")
            return ok, f"HTTP {resp.status} {body[:60]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _notify(text: str) -> str:
    phones = [p.strip() for p in (os.environ.get("PM_ALERT_PHONES") or "").split(",") if p.strip()]
    if not phones:
        print("[info] 未配置 PM_ALERT_PHONES，跳过短信告警")
        return "no-phone"
    sys.path.insert(0, str(PROJECT_ROOT))
    from ledger_app.sms import send_sms, sms_configured

    if not sms_configured():
        print("[info] 短信未配置，跳过告警")
        return "not-configured"
    for phone in phones:
        res = send_sms(phone, 1)  # 模板要求 ${number}，这里给 1 占位
        print(f"[alert] {phone}: {'OK' if res.get('ok') else res.get('code')}")
    return "sent"


def main() -> int:
    url = os.environ.get("PM_HEALTH_URL") or DEFAULT_URL
    threshold = int(os.environ.get("PM_ALERT_FAILS") or 3)
    state = _load_state()
    ok, detail = _probe(url)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if ok:
        if state.get("alerted"):
            print("[recover] 服务已恢复")
            _notify("项目账本服务已恢复正常。")
        state = {"failures": 0, "alerted": False, "last_ok": now.isoformat()}
        _save_state(state)
        print(f"[ok] {detail}")
        return 0

    failures = int(state.get("failures") or 0) + 1
    state["failures"] = failures
    state["last_error"] = detail
    state["last_fail_at"] = now.isoformat()
    print(f"[fail] 第 {failures} 次失败：{detail}")

    if failures >= threshold:
        last_alert = state.get("last_alert_at")
        cooled = True
        if last_alert:
            try:
                cooled = (now - datetime.fromisoformat(last_alert)) > timedelta(hours=1)
            except ValueError:
                cooled = True
        if cooled:
            if _notify(f"【告警】项目账本服务连续 {failures} 次健康检查失败：{detail}") == "sent":
                state["alerted"] = True
                state["last_alert_at"] = now.isoformat()
    _save_state(state)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
