#!/usr/bin/env python3
"""校验从服务器同步下来的数据是否完整、自洽。

检查项：
  1. SQLite integrity_check / foreign_key_check
  2. 各表行数，并与 manifest.json（服务器端生成）逐表比对
  3. attachments / project_update_attachments 登记的 stored_path 是否都存在于 uploads/
  4. uploads/ 下未被任何附件记录引用的文件（孤儿文件，仅报告）
  5. manifest.json 记录的 db 与每个附件的 sha256 是否与本地一致（传输完整性）

用法：
  "F:\\Anaconda3\\python.exe" scripts\\verify_data_integrity.py ^
      --db _aliyun_snapshot\\ledger.db ^
      --uploads _aliyun_snapshot\\uploads ^
      --manifest _aliyun_snapshot\\manifest.json

退出码：0=通过（可能带警告）；1=发现错误；2=无法读取输入。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

CHUNK = 1024 * 1024

# 登记附件的表 -> 指向 uploads 的相对路径列
ATTACHMENT_TABLES = ("attachments", "project_update_attachments")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        print(f"  [错误] {msg}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"  [警告] {msg}")

    def ok(self, msg: str) -> None:
        print(f"  [通过] {msg}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
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
        counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0]
    return counts


def check_db(db_path: Path, manifest: dict | None, rep: Report) -> dict[str, int]:
    print(f"\n[1] 数据库 {db_path}")
    if not db_path.is_file():
        rep.error(f"数据库文件不存在：{db_path}")
        return {}

    if manifest and manifest.get("db", {}).get("sha256"):
        expect = manifest["db"]["sha256"]
        actual = sha256_file(db_path)
        if expect == actual:
            rep.ok(f"sha256 与服务器一致 ({actual[:16]}…)")
        else:
            rep.error(f"sha256 与服务器不一致：期望 {expect[:16]}…，实际 {actual[:16]}…")

    conn = sqlite3.connect(str(db_path))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity == "ok":
            rep.ok("integrity_check = ok")
        else:
            rep.error(f"integrity_check 异常：{integrity}")

        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if not fk:
            rep.ok("foreign_key_check 无违规")
        else:
            rep.error(f"foreign_key_check 有 {len(fk)} 条违规，例如 {fk[:3]}")

        counts = table_counts(conn)

        # 关键业务汇总（便于人工核对金额是否与线上一致）
        try:
            active = dict(
                conn.execute(
                    "SELECT type, COALESCE(SUM(amount_cents),0) FROM transactions "
                    "WHERE status='active' AND is_void=0 GROUP BY type"
                ).fetchall()
            )
            pending = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE status<>'active'"
            ).fetchone()[0]
            voided = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE is_void=1"
            ).fetchone()[0]
            income = int(active.get("income", 0))
            expense = int(active.get("expense", 0))
            print(
                f"  [信息] 有效流水：收入 {income / 100:.2f} 元，"
                f"支出 {expense / 100:.2f} 元，净额 {(income - expense) / 100:.2f} 元"
                f"（待审批 {pending} 笔，作废 {voided} 笔）"
            )
        except sqlite3.Error as exc:  # pragma: no cover - 结构异常时的兜底
            rep.warn(f"无法汇总流水金额：{exc}")
    finally:
        conn.close()

    if manifest:
        expect_counts = manifest.get("db", {}).get("table_counts") or {}
        if expect_counts:
            diffs = {
                table: (expect_counts.get(table), counts.get(table))
                for table in set(expect_counts) | set(counts)
                if expect_counts.get(table) != counts.get(table)
            }
            if diffs:
                for table, (want, got) in sorted(diffs.items()):
                    rep.error(f"表 {table} 行数不一致：服务器 {want}，本地 {got}")
            else:
                rep.ok(f"{len(counts)} 张表行数与服务器完全一致")
        else:
            rep.warn("manifest 中没有行数信息，跳过行数比对")

    return counts


def check_attachments(
    db_path: Path, uploads_dir: Path, manifest: dict | None, rep: Report
) -> None:
    print(f"\n[2] 附件 {uploads_dir}")
    conn = sqlite3.connect(str(db_path))
    try:
        registered: dict[str, str] = {}
        for table in ATTACHMENT_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                rep.warn(f"表 {table} 不存在，跳过")
                continue
            rows = conn.execute(
                f"SELECT stored_path FROM {table} WHERE stored_path IS NOT NULL"
            ).fetchall()
            for (stored_path,) in rows:
                registered[str(stored_path).replace("\\", "/")] = table
            print(f"  [信息] {table}: {len(rows)} 条登记记录")
    finally:
        conn.close()

    if not uploads_dir.is_dir():
        if registered:
            rep.error(f"uploads 目录不存在，但数据库登记了 {len(registered)} 个附件")
        else:
            rep.warn(f"uploads 目录不存在：{uploads_dir}")
        return

    on_disk = {
        p.relative_to(uploads_dir).as_posix()
        for p in uploads_dir.rglob("*")
        if p.is_file()
    }

    missing = sorted(set(registered) - on_disk)
    if missing:
        rep.error(
            f"{len(missing)} 个附件在数据库中有记录但磁盘缺失，例如：{missing[:5]}"
        )
    else:
        rep.ok(f"数据库登记的 {len(registered)} 个附件在磁盘上全部存在")

    orphans = sorted(on_disk - set(registered))
    if orphans:
        rep.warn(
            f"{len(orphans)} 个文件在 uploads 中但未被数据库引用（历史残留或旧命名），"
            f"例如：{orphans[:5]}"
        )
    else:
        rep.ok("uploads 中没有未被引用的文件")

    if manifest:
        files = manifest.get("uploads", {}).get("files") or []
        expect_count = manifest.get("uploads", {}).get("count")
        if expect_count is not None and expect_count != len(on_disk):
            rep.error(f"文件总数不一致：服务器 {expect_count}，本地 {len(on_disk)}")
        else:
            rep.ok(f"文件总数一致（{len(on_disk)}）")

        bad_hash = []
        bad_size = []
        for item in files:
            local = uploads_dir / item["relpath"]
            if not local.is_file():
                bad_hash.append((item["relpath"], "缺失"))
                continue
            if local.stat().st_size != item["size"]:
                bad_size.append(item["relpath"])
                continue
            if sha256_file(local) != item["sha256"]:
                bad_hash.append((item["relpath"], "sha256 不符"))
        if bad_size:
            rep.error(f"{len(bad_size)} 个文件大小与服务器不一致，例如：{bad_size[:5]}")
        if bad_hash:
            rep.error(f"{len(bad_hash)} 个文件校验失败，例如：{bad_hash[:5]}")
        if not bad_size and not bad_hash:
            rep.ok(f"{len(files)} 个文件的 sha256 全部与服务器一致")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - 仅影响输出编码
        pass

    parser = argparse.ArgumentParser(description="校验同步下来的项目数据")
    parser.add_argument("--db", required=True, help="本地 ledger.db 路径")
    parser.add_argument("--uploads", required=True, help="本地 uploads 目录")
    parser.add_argument("--manifest", help="服务器端生成的 manifest.json（可选）")
    args = parser.parse_args()

    db_path = Path(args.db)
    uploads_dir = Path(args.uploads)

    manifest = None
    if args.manifest:
        manifest_path = Path(args.manifest)
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            print(f"[警告] 找不到 manifest：{manifest_path}，跳过与服务器比对")
            return 2

    if not db_path.is_file():
        print(f"[错误] 数据库不存在：{db_path}")
        return 2

    rep = Report()
    counts = check_db(db_path, manifest, rep)
    if counts:
        check_attachments(db_path, uploads_dir, manifest, rep)

    print("\n===== 校验结果 =====")
    print(f"错误 {len(rep.errors)} 项，警告 {len(rep.warnings)} 项")
    if rep.errors:
        print("数据存在问题，请勿覆盖本地/线上数据，先排查后再继续。")
        return 1
    print("数据完整且自洽，可以安全使用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
