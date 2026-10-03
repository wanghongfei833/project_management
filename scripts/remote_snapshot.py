#!/usr/bin/env python3
"""在部署机上生成项目数据的一致快照：SQLite 热备份 + uploads 逐文件清单。

ledger.db 由 gunicorn 常驻读写，直接 scp 数据库文件可能拿到「写入一半」的库，
或缺失 -wal/-shm 的不一致副本。本脚本用 sqlite3 的 backup API 生成一致快照，
并记录 integrity_check、foreign_key_check、各表行数，以及 uploads 下每个文件的
sha256，供下载后在本地逐项比对。

用法（在服务器上执行）：
    /root/miniconda3/envs/TIE/bin/python remote_snapshot.py \
        --root /root/project/PM --out /tmp/pm_snapshot
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

CHUNK = 1024 * 1024


def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def make_db_snapshot(src: Path, dst: Path) -> dict:
    """用 backup API 生成一致快照，并返回快照的自检信息。"""
    if not src.is_file():
        raise SystemExit(f"[error] 数据库不存在：{src}")
    if dst.exists():
        dst.unlink()

    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    conn = sqlite3.connect(str(dst))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {}
        for table in tables:
            quoted = table.replace('"', '""')
            counts[table] = conn.execute(
                f'SELECT COUNT(*) FROM "{quoted}"'
            ).fetchone()[0]
    finally:
        conn.close()

    return {
        "file": dst.name,
        "size": dst.stat().st_size,
        "sha256": sha256_file(dst),
        "journal_mode": journal_mode,
        "integrity_check": integrity,
        "foreign_key_violations": len(fk_violations),
        "table_counts": counts,
    }


def scan_uploads(root: Path) -> dict:
    base = root / "uploads"
    files: list[dict] = []
    if not base.is_dir():
        log(f"[warn] 未找到上传目录：{base}")
        return {"dir": str(base), "count": 0, "total_bytes": 0, "files": files}

    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        st = path.stat()
        files.append(
            {
                "relpath": path.relative_to(base).as_posix(),
                "size": st.st_size,
                "sha256": sha256_file(path),
            }
        )

    return {
        "dir": str(base),
        "count": len(files),
        "total_bytes": sum(item["size"] for item in files),
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成项目数据一致快照")
    parser.add_argument("--root", required=True, help="服务器上的项目根目录")
    parser.add_argument("--out", required=True, help="快照输出目录")
    parser.add_argument(
        "--db", default="instance/ledger.db", help="相对项目根的数据库路径"
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    log(f"[1/3] 备份数据库 {root / args.db}")
    db_info = make_db_snapshot(root / args.db, out / "ledger.db")
    log(
        f"      integrity_check={db_info['integrity_check']} "
        f"fk_violations={db_info['foreign_key_violations']} "
        f"tables={len(db_info['table_counts'])} "
        f"size={db_info['size']}"
    )
    if db_info["integrity_check"] != "ok":
        log("[error] 快照未通过 integrity_check，请先排查服务器数据库，不要直接覆盖本地数据")
        return 2

    log("[2/3] 扫描 uploads 并计算 sha256")
    uploads_info = scan_uploads(root)
    log(
        f"      文件数={uploads_info['count']} "
        f"总字节={uploads_info['total_bytes']}"
    )

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server_root": str(root),
        "db": db_info,
        "uploads": uploads_info,
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"[3/3] 已写出 {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
