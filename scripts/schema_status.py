#!/usr/bin/env python3
"""查看数据库迁移状态：已应用哪些版本、还有哪些没跑。

用法（服务器）::

    cd /root/project/PM
    DATABASE_URL="sqlite:////root/project/PM/instance/ledger.db" UPLOAD_FOLDER=uploads SECRET_KEY=x \
      /root/miniconda3/envs/TIE/bin/python scripts/schema_status.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    from run import create_app
    from ledger_app.extensions import db
    from ledger_app.schema import MIGRATIONS, applied_migrations, ensure_sqlite_schema

    app = create_app()
    with app.app_context():
        before = applied_migrations()
        print("数据库迁移状态")
        print("  schema_migrations 已记录 %d 条：" % len(before))
        for version, note, _fn in MIGRATIONS:
            state = before.get(version)
            print("   %-6s %-46s %s" % (version, note, ("已应用 " + state[:19]) if state else "未应用"))
        pending = [v for v, _n, _f in MIGRATIONS if v not in before]
        if pending:
            print("\n发现未应用的迁移：%s，正在执行…" % ", ".join(pending))
            newly = ensure_sqlite_schema()
            print("已执行：%s" % (", ".join(newly) or "无"))
        else:
            print("\n全部迁移已应用。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
