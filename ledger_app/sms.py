"""阿里云短信（Dysmsapi 2017-05-25）发送封装。

只用标准库实现 RPC 签名，不引入额外依赖；密钥等从环境变量读取：

- ``ALIYUN_SMS_ACCESS_KEY_ID``     访问密钥 ID（必填）
- ``ALIYUN_SMS_ACCESS_KEY_SECRET`` 访问密钥 Secret（必填）
- ``ALIYUN_SMS_SIGN_NAME``         短信签名名称（必填，例如「成都星瞳科技」）
- ``ALIYUN_SMS_TEMPLATE_CODE``     模板 CODE，默认 ``SMS_512630691``
- ``ALIYUN_SMS_REGION``            区域，默认 ``cn-hangzhou``
- ``ALIYUN_SMS_ENDPOINT``          接口地址，默认 ``https://dysmsapi.aliyuncs.com``

未配置时 ``send_sms`` 直接返回 ``NOT_CONFIGURED``，不会发短信也不会抛异常。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

DEFAULT_TEMPLATE_CODE = "SMS_512630691"
# 阿里云短信模板文案（${number} 为待处理条数）
SMS_TEMPLATE_TEXT = "项目账本还有{number}条内容未处理，请及时登录仪表盘 - 项目账本进行处理。"

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def normalize_phone(raw) -> str:
    """去掉空格/横线/+86 前缀，返回纯数字号码。"""
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    return digits


def is_valid_phone(raw) -> bool:
    return bool(_PHONE_RE.match(normalize_phone(raw)))


def allowed_phones() -> set[str]:
    """测试白名单：``ALIYUN_SMS_ALLOW_PHONES`` 非空时，自动提醒只发给这些号码。

    例如 ``ALIYUN_SMS_ALLOW_PHONES=15682527196``：先只给一个人试，确认没问题
    再清空这个变量全量放开。留空/不设置＝不限制。
    """
    raw = os.environ.get("ALIYUN_SMS_ALLOW_PHONES") or ""
    phones = set()
    for part in re.split(r"[,，;；\s]+", raw):
        p = normalize_phone(part)
        if p:
            phones.add(p)
    return phones


def mask_phone(raw) -> str:
    """156****7196（仅用于界面展示）。"""
    p = normalize_phone(raw)
    if len(p) != 11:
        return p or "—"
    return f"{p[:3]}****{p[-4:]}"


def render_pending_text(number: int) -> str:
    return SMS_TEMPLATE_TEXT.format(number=int(number))


def sms_config() -> dict:
    return {
        "access_key_id": (os.environ.get("ALIYUN_SMS_ACCESS_KEY_ID") or "").strip(),
        "access_key_secret": (os.environ.get("ALIYUN_SMS_ACCESS_KEY_SECRET") or "").strip(),
        "sign_name": (os.environ.get("ALIYUN_SMS_SIGN_NAME") or "").strip(),
        "template_code": (os.environ.get("ALIYUN_SMS_TEMPLATE_CODE") or "").strip()
        or DEFAULT_TEMPLATE_CODE,
        "region_id": (os.environ.get("ALIYUN_SMS_REGION") or "").strip() or "cn-hangzhou",
        "endpoint": (os.environ.get("ALIYUN_SMS_ENDPOINT") or "").strip()
        or "https://dysmsapi.aliyuncs.com",
    }


def sms_configured(cfg: dict | None = None) -> bool:
    cfg = cfg or sms_config()
    return bool(
        cfg.get("access_key_id")
        and cfg.get("access_key_secret")
        and cfg.get("sign_name")
        and cfg.get("template_code")
    )


def _percent_encode(value) -> str:
    """阿里云 RPC 签名的特殊转义规则。"""
    return (
        quote(str(value), safe="~")
        .replace("+", "%20")
        .replace("*", "%2A")
        .replace("%7E", "~")
    )


def _sign(params: dict, access_key_secret: str) -> str:
    canonical = "&".join(
        f"{_percent_encode(k)}={_percent_encode(params[k])}" for k in sorted(params)
    )
    string_to_sign = "GET&%2F&" + _percent_encode(canonical)
    digest = hmac.new(
        (access_key_secret + "&").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def send_sms(
    phone: str,
    number: int,
    *,
    template_code: str | None = None,
    timeout: int = 10,
) -> dict:
    """发送一条「待办提醒」短信。返回 dict（不抛异常）。

    返回::

        {"ok": bool, "code": str, "message": str, "request_id": str,
         "biz_id": str, "content": str, "configured": bool}
    """
    cfg = sms_config()
    target = normalize_phone(phone)
    content = render_pending_text(number)
    result = {
        "ok": False,
        "code": "",
        "message": "",
        "request_id": "",
        "biz_id": "",
        "content": content,
        "configured": sms_configured(cfg),
        "phone": target,
        "template_code": template_code or cfg["template_code"],
    }

    if not result["configured"]:
        result["code"] = "NOT_CONFIGURED"
        result["message"] = "短信未配置（缺少 ALIYUN_SMS_ACCESS_KEY_ID/SECRET/SIGN_NAME）"
        return result
    if not is_valid_phone(target):
        result["code"] = "INVALID_PHONE"
        result["message"] = f"手机号不合法：{target or '(空)'}"
        return result

    params = {
        "Action": "SendSms",
        "Version": "2017-05-25",
        "Format": "JSON",
        "RegionId": cfg["region_id"],
        "AccessKeyId": cfg["access_key_id"],
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "PhoneNumbers": target,
        "SignName": cfg["sign_name"],
        "TemplateCode": result["template_code"],
        "TemplateParam": json.dumps({"number": str(int(number))}, ensure_ascii=False),
    }
    params["Signature"] = _sign(params, cfg["access_key_secret"])

    url = cfg["endpoint"].rstrip("/") + "/?" + urlencode(params)
    try:
        req = Request(url, method="GET", headers={"User-Agent": "pm-ledger/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:  # 网络异常、超时、返回非 JSON 等
        result["code"] = "REQUEST_FAILED"
        result["message"] = f"{type(exc).__name__}: {exc}"
        return result

    result["code"] = str(payload.get("Code") or "")
    result["message"] = str(payload.get("Message") or "")
    result["request_id"] = str(payload.get("RequestId") or "")
    result["biz_id"] = str(payload.get("BizId") or "")
    result["ok"] = result["code"] == "OK"
    return result
