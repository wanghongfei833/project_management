"""阿里云短信（Dysmsapi 2017-05-25）发送封装。

只用标准库实现 RPC 签名，不引入额外依赖；密钥等从环境变量读取：

- ``ALIYUN_SMS_ACCESS_KEY_ID``     访问密钥 ID（必填）
- ``ALIYUN_SMS_ACCESS_KEY_SECRET`` 访问密钥 Secret（必填）
- ``ALIYUN_SMS_SIGN_NAME``         短信签名名称（必填，例如「成都星瞳科技」）
- ``ALIYUN_SMS_TEMPLATE_CODE``     模板 CODE，默认 ``SMS_512571109``
- ``ALIYUN_SMS_PARAM_COUNT``       条数变量名，默认 ``n1``
- ``ALIYUN_SMS_PARAM_HOURS``       小时变量名，默认 ``n2``
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

DEFAULT_TEMPLATE_CODE = "SMS_512571109"
# 阿里云短信模板文案（${n1} = 未做条数，${n2} = 已过去小时数）
# 注意：真正发出去的内容以阿里云后台登记的模板为准，这里只用于页面/记录展示。
SMS_TEMPLATE_TEXT = "提醒您，星瞳账本还有{n1}条事项未做(已过去{n2}小时"

# 模板变量名（阿里云会校验变量名与模板是否匹配，换模板时可用环境变量覆盖）
DEFAULT_PARAM_COUNT = "n1"
DEFAULT_PARAM_HOURS = "n2"

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


def render_pending_text(number: int, hours: int = 0) -> str:
    return SMS_TEMPLATE_TEXT.format(n1=int(number), n2=int(hours))


def sms_config() -> dict:
    return {
        "access_key_id": (os.environ.get("ALIYUN_SMS_ACCESS_KEY_ID") or "").strip(),
        "access_key_secret": (os.environ.get("ALIYUN_SMS_ACCESS_KEY_SECRET") or "").strip(),
        "sign_name": (os.environ.get("ALIYUN_SMS_SIGN_NAME") or "").strip(),
        "template_code": (os.environ.get("ALIYUN_SMS_TEMPLATE_CODE") or "").strip()
        or DEFAULT_TEMPLATE_CODE,
        "region_id": (os.environ.get("ALIYUN_SMS_REGION") or "").strip() or "cn-hangzhou",
        "param_count": (os.environ.get("ALIYUN_SMS_PARAM_COUNT") or "").strip()
        or DEFAULT_PARAM_COUNT,
        "param_hours": (os.environ.get("ALIYUN_SMS_PARAM_HOURS") or "").strip()
        or DEFAULT_PARAM_HOURS,
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
    hours: int | None = None,
    template_code: str | None = None,
    timeout: int = 10,
) -> dict:
    """发送一条「待办/日志提醒」短信。返回 dict（不抛异常）。

    ``number`` → 模板变量 n1（未做条数）；``hours`` → 模板变量 n2（已过去小时数）。

    返回::

        {"ok": bool, "code": str, "message": str, "request_id": str,
         "biz_id": str, "content": str, "configured": bool}
    """
    cfg = sms_config()
    target = normalize_phone(phone)
    hours_value = int(hours) if hours is not None else 0
    content = render_pending_text(number, hours_value)
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
        "TemplateParam": json.dumps(
            {
                cfg["param_count"]: str(int(number)),
                cfg["param_hours"]: str(hours_value),
            },
            ensure_ascii=False,
        ),
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


def _call_api(action: str, extra_params: dict, *, timeout: int = 10) -> dict:
    """调用阿里云短信 OpenAPI（RPC 签名），返回解析后的 JSON（失败时带 __error）。"""
    cfg = sms_config()
    if not sms_configured(cfg):
        return {"__error": "NOT_CONFIGURED", "__message": "短信未配置"}
    params = {
        "Action": action,
        "Version": "2017-05-25",
        "Format": "JSON",
        "RegionId": cfg["region_id"],
        "AccessKeyId": cfg["access_key_id"],
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    params.update(extra_params)
    params["Signature"] = _sign(params, cfg["access_key_secret"])
    url = cfg["endpoint"].rstrip("/") + "/?" + urlencode(params)
    try:
        req = Request(url, method="GET", headers={"User-Agent": "pm-ledger/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:
        return {"__error": type(exc).__name__, "__message": str(exc)}


def query_send_details(
    phone: str, send_date: str, *, page_size: int = 50, page: int = 1, biz_id: str = ""
) -> dict:
    """查询某号码某天的短信**投递回执**（阿里云控制台「发送记录」的接口版）。

    ``send_date`` 形如 ``20261011``。返回 ``{"ok", "code", "message", "records": [...]}``，
    每条记录含 send_status（1 等待回执 / 2 发送失败 / 3 发送成功）、err_code、content、receive_date。
    """
    params = {
        "PhoneNumber": normalize_phone(phone),
        "SendDate": str(send_date),
        "PageSize": int(page_size),
        "CurrentPage": int(page),
    }
    if biz_id:
        params["BizId"] = str(biz_id)
    payload = _call_api("QuerySendDetails", params)
    if "__error" in payload:
        return {"ok": False, "code": payload["__error"], "message": payload.get("__message", ""), "records": []}

    details = (payload.get("SmsSendDetailDTOs") or {}).get("SmsSendDetailDTO") or []
    if isinstance(details, dict):  # 只有一条时阿里云返回 dict
        details = [details]
    records = []
    for d in details:
        status = int(d.get("SendStatus") or 0)
        records.append(
            {
                "send_status": status,
                # 统一用短标签：库里/页面上都按「成功 / 失败 / 等待回执」判断
                "status_text": {1: "等待回执", 2: "失败", 3: "成功"}.get(status, f"未知({status})"),
                "err_code": (d.get("ErrCode") or "").strip(),
                "phone": d.get("PhoneNum"),
                "content": d.get("Content"),
                "send_date": d.get("SendDate"),
                "receive_date": d.get("ReceiveDate"),
                "template_code": d.get("TemplateCode"),
                "out_id": d.get("OutId"),
            }
        )
    return {
        "ok": str(payload.get("Code") or "") == "OK",
        "code": payload.get("Code") or "",
        "message": payload.get("Message") or "",
        "total": int(payload.get("TotalCount") or 0),
        "records": records,
    }


# 常见错误码 → 人话解释（页面/脚本展示用）
ERROR_HINTS = {
    "PORT_NOT_REGISTERED": "该号段运营商通道未开通（例如移动还在报备中）",
    "MOBILE_NOT_ON_SERVICE": "号码不在服务区 / 已停机",
    "CONTENT_KEYWORD": "内容含被运营商拦截的关键词",
    "isv.BUSINESS_LIMIT_CONTROL": "触发流控（同号码/同签名发送频率限制）",
    "isv.SMS_SIGNATURE_ILLEGAL": "签名名称与阿里云控制台不一致或未审核",
    "isv.SMS_TEMPLATE_ILLEGAL": "模板 CODE 与参数不匹配或未审核",
    "isv.AMOUNT_NOT_ENOUGH": "阿里云短信余额不足",
    "NOT_CONFIGURED": "本系统未配置短信密钥",
    "INVALID_PHONE": "手机号格式不对",
    "REQUEST_FAILED": "调用阿里云接口失败（网络/超时）",
}


def error_hint(code: str | None) -> str:
    """把错误码翻译成一句人话（没收录的返回空串）。"""
    return ERROR_HINTS.get((code or "").strip(), "")
