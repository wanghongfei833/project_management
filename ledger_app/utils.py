from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO


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

