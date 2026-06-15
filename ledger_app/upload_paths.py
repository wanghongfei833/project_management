"""凭证在磁盘上的相对路径与原始文件名（支持中文等非 ASCII 展示名）。"""

from __future__ import annotations

import re
from pathlib import Path

# 磁盘文件后缀白名单（小写）；其余归为 .bin，避免奇怪扩展名
ALLOWED_SUFFIXES = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".rar",
        ".7z",
        ".txt",
        ".csv",
        ".md",
    }
)


def attachment_display_name(upload_filename: str | None) -> str:
    """用于界面与下载展示的文件名，保留 Unicode，去掉路径与非法字符。"""
    if not upload_filename:
        return "file"
    name = Path(str(upload_filename)).name.replace("\x00", "").strip()
    if not name or name in (".", ".."):
        return "file"
    return name[:250]


def attachment_disk_suffix(display_name: str) -> str:
    suf = Path(display_name).suffix.lower()
    if suf in ALLOWED_SUFFIXES:
        return suf
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suf):
        return suf
    return ".bin"


def transaction_attachment_relpath(
    project_id: int, transaction_id: int, digest_hex: str, display_name: str
) -> str:
    suffix = attachment_disk_suffix(display_name)
    d = digest_hex[:16]
    return f"projects/{int(project_id)}/t{int(transaction_id)}_{d}{suffix}"


def project_update_attachment_relpath(
    project_id: int, update_id: int, digest_hex: str, display_name: str
) -> str:
    suffix = attachment_disk_suffix(display_name)
    d = digest_hex[:16]
    return f"projects/{int(project_id)}/updates/u{int(update_id)}_{d}{suffix}"
