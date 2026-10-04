from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO


def format_cents(cents) -> str:
    """把「分」格式化成带千分位的元字符串，例如 30000000 -> '300,000.00'。

    仅用于界面展示/消息文本；金额的计算与存储始终用「分」的整数。
    """
    try:
        value = Decimal(int(cents or 0)) / Decimal(100)
    except (TypeError, ValueError):
        return "0.00"
    return f"{value:,.2f}"


def format_dt_local(value) -> str:
    """库里存的是 naive UTC，这里转成东八区字符串（仅用于界面展示）。"""
    if not value:
        return "—"
    try:
        return (value + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    except TypeError:
        return str(value)


def sha256_file(f: BinaryIO) -> str:
    h = hashlib.sha256()
    while True:
        chunk = f.read(1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def safe_join_upload(base_dir: str, filename: str) -> str:
    p = (Path(base_dir) / filename).resolve()
    base = Path(base_dir).resolve()
    if base not in p.parents and base != p:
        raise ValueError("invalid upload path")
    return str(p)
