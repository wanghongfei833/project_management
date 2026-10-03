#!/usr/bin/env python3
"""清理 uploads 下未被数据库引用的孤儿文件（默认只预览，--apply 才移动）。

孤儿文件 = 磁盘上存在、但没有任何附件记录（attachments /
project_update_attachments 的 stored_path）指向它的文件。常见来源是删除流水时
没有连带清理磁盘文件。

为了不丢数据，脚本只做「移动」：把孤儿文件按原相对路径挪到 --dest 目录
（默认 <repo>/_aliyun_data/orphans）。移动前请确保数据已备份。

用法（本项目环境）：
  & 'F:\\Anaconda3\\envs\\aliyun\\python.exe' -X utf8 scripts\\prune_orphan_uploads.py
  & 'F:\\Anaconda3\\envs\\aliyun\\python.exe' -X utf8 scripts\\prune_orphan_uploads.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

ATTACHMENT_TABLES = ("attachments", "project_update_attachments")


def referenced_paths(conn: sqlite3.Connection) -> set[str]:
    """所有附件记录指向的相对路径（统一成 posix 形式）。"""
    refs: set[str] = set()
    for table in ATTACHMENT_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        for (stored_path,) in conn.execute(
            f"SELECT stored_path FROM {table} WHERE stored_path IS NOT NULL"
        ):
            refs.add(str(stored_path).replace("\\", "/"))
    return refs


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 uploads 下的孤儿文件")
    parser.add_argument("--db", default="instance/ledger.db", help="数据库路径")
    parser.add_argument("--uploads", default="uploads", help="附件根目录")
    parser.add_argument(
        "--dest", default="_aliyun_data/orphans", help="孤儿文件归档目录"
    )
    parser.add_argument("--apply", action="store_true", help="真正移动；默认只预览")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    db = Path(args.db) if Path(args.db).is_absolute() else repo / args.db
    uploads = (
        Path(args.uploads) if Path(args.uploads).is_absolute() else repo / args.uploads
    )
    dest = Path(args.dest) if Path(args.dest).is_absolute() else repo / args.dest

    if not db.is_file():
        print(f"[错误] 数据库不存在：{db}")
        return 2
    if not uploads.is_dir():
        print(f"[错误] uploads 目录不存在：{uploads}")
        return 2

    conn = sqlite3.connect(str(db))
    try:
        refs = referenced_paths(conn)
    finally:
        conn.close()

    on_disk = {
        p.relative_to(uploads).as_posix() for p in uploads.rglob("*") if p.is_file()
    }
    orphans = sorted(on_disk - refs)
    missing = sorted(refs - on_disk)

    print(f"数据库引用 {len(refs)} 个附件；磁盘 {len(on_disk)} 个文件")
    print(f"孤儿文件 {len(orphans)} 个；有记录但磁盘缺失 {len(missing)} 个")
    if missing:
        print(f"  [警告] 有记录但磁盘缺失：{missing}")
    for rel in orphans:
        print(f"  [孤儿] {rel}")

    if not orphans:
        print("没有需要清理的孤儿文件。")
        return 0

    if not args.apply:
        print(f"\n预览模式：未改动任何文件。加 --apply 会把上述文件移动到 {dest}")
        return 0

    moved = 0
    for rel in orphans:
        src = uploads / rel
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        moved += 1

    # 清掉移动后留下的空目录
    for d in sorted((p for p in uploads.rglob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass

    remain = len([p for p in uploads.rglob("*") if p.is_file()])
    print(f"\n已移动 {moved} 个孤儿文件到 {dest}")
    print(f"uploads 剩余文件：{remain}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - 仅影响输出编码
        pass
    sys.exit(main())
