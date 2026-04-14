from __future__ import annotations

from sqlalchemy import text

from .extensions import db


def _table_exists(table_name: str) -> bool:
    sql = text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=:name LIMIT 1"
    )
    return db.session.execute(sql, {"name": table_name}).scalar() is not None


def _column_exists(table_name: str, column_name: str) -> bool:
    rows = db.session.execute(text(f"PRAGMA table_info({table_name})")).mappings().all()
    return any(r.get("name") == column_name for r in rows)


def ensure_sqlite_schema():
    """
    Lightweight, automatic SQLite migrations.

    This keeps the project simple (no Alembic/Migrate) while preventing
    OperationalError when models add new columns/tables.
    """
    # Add projects.referral_ratio if missing
    if _table_exists("projects") and not _column_exists("projects", "referral_ratio"):
        db.session.execute(
            text("ALTER TABLE projects ADD COLUMN referral_ratio NUMERIC NOT NULL DEFAULT 0")
        )

    if _table_exists("projects") and not _column_exists("projects", "broker_fee_mode"):
        db.session.execute(
            text(
                "ALTER TABLE projects ADD COLUMN broker_fee_mode VARCHAR(16) "
                "NOT NULL DEFAULT 'percent'"
            )
        )
    if _table_exists("projects") and not _column_exists("projects", "broker_fee_direction"):
        db.session.execute(
            text(
                "ALTER TABLE projects ADD COLUMN broker_fee_direction VARCHAR(32) "
                "NOT NULL DEFAULT 'we_pay_separate'"
            )
        )
    if _table_exists("projects") and not _column_exists("projects", "broker_fixed_fee_cents"):
        db.session.execute(
            text(
                "ALTER TABLE projects ADD COLUMN broker_fixed_fee_cents BIGINT NOT NULL DEFAULT 0"
            )
        )

    # Create project_expected_income_adjustments table if missing
    if not _table_exists("project_expected_income_adjustments"):
        db.session.execute(
            text(
                """
                CREATE TABLE project_expected_income_adjustments (
                  id INTEGER PRIMARY KEY,
                  project_id INTEGER NOT NULL,
                  amount_cents BIGINT NOT NULL,
                  note TEXT,
                  created_by_user_id INTEGER,
                  created_at DATETIME NOT NULL,
                  FOREIGN KEY(project_id) REFERENCES projects(id),
                  FOREIGN KEY(created_by_user_id) REFERENCES users(id)
                )
                """
            )
        )
        db.session.execute(
            text(
                "CREATE INDEX ix_project_expected_income_adjustments_project_id "
                "ON project_expected_income_adjustments (project_id)"
            )
        )

    # Project members
    if not _table_exists("project_members"):
        db.session.execute(
            text(
                """
                CREATE TABLE project_members (
                  project_id INTEGER NOT NULL,
                  user_id INTEGER NOT NULL,
                  created_at DATETIME NOT NULL,
                  PRIMARY KEY (project_id, user_id),
                  FOREIGN KEY(project_id) REFERENCES projects(id),
                  FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
        )
        db.session.execute(text("CREATE INDEX ix_project_members_project_id ON project_members (project_id)"))
        db.session.execute(text("CREATE INDEX ix_project_members_user_id ON project_members (user_id)"))

    # Delete request + approvals
    if not _table_exists("project_delete_requests"):
        db.session.execute(
            text(
                """
                CREATE TABLE project_delete_requests (
                  id INTEGER PRIMARY KEY,
                  project_id INTEGER NOT NULL,
                  status VARCHAR(16) NOT NULL DEFAULT 'open',
                  created_by_user_id INTEGER,
                  created_at DATETIME NOT NULL,
                  executed_at DATETIME,
                  executed_by_user_id INTEGER,
                  FOREIGN KEY(project_id) REFERENCES projects(id),
                  FOREIGN KEY(created_by_user_id) REFERENCES users(id),
                  FOREIGN KEY(executed_by_user_id) REFERENCES users(id)
                )
                """
            )
        )
        db.session.execute(text("CREATE INDEX ix_project_delete_requests_project_id ON project_delete_requests (project_id)"))

    if not _table_exists("project_delete_approvals"):
        db.session.execute(
            text(
                """
                CREATE TABLE project_delete_approvals (
                  request_id INTEGER NOT NULL,
                  user_id INTEGER NOT NULL,
                  approved_at DATETIME NOT NULL,
                  PRIMARY KEY (request_id, user_id),
                  FOREIGN KEY(request_id) REFERENCES project_delete_requests(id),
                  FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
        )

    # Transaction edit requests + approvals
    if not _table_exists("transaction_edit_requests"):
        db.session.execute(
            text(
                """
                CREATE TABLE transaction_edit_requests (
                  id INTEGER PRIMARY KEY,
                  transaction_id INTEGER NOT NULL,
                  project_id INTEGER NOT NULL,
                  status VARCHAR(16) NOT NULL DEFAULT 'open',
                  new_type VARCHAR(16) NOT NULL,
                  new_amount_cents BIGINT NOT NULL,
                  new_occur_date DATE NOT NULL,
                  new_settled BOOLEAN NOT NULL,
                  new_counterparty VARCHAR(256),
                  new_note TEXT,
                  created_by_user_id INTEGER,
                  created_at DATETIME NOT NULL,
                  executed_at DATETIME,
                  executed_by_user_id INTEGER,
                  FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE,
                  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                  FOREIGN KEY(created_by_user_id) REFERENCES users(id),
                  FOREIGN KEY(executed_by_user_id) REFERENCES users(id)
                )
                """
            )
        )
        db.session.execute(
            text(
                "CREATE INDEX ix_transaction_edit_requests_transaction_id "
                "ON transaction_edit_requests (transaction_id)"
            )
        )
        db.session.execute(
            text(
                "CREATE INDEX ix_transaction_edit_requests_project_id "
                "ON transaction_edit_requests (project_id)"
            )
        )

    if not _table_exists("transaction_edit_approvals"):
        db.session.execute(
            text(
                """
                CREATE TABLE transaction_edit_approvals (
                  request_id INTEGER NOT NULL,
                  user_id INTEGER NOT NULL,
                  approved_at DATETIME NOT NULL,
                  PRIMARY KEY (request_id, user_id),
                  FOREIGN KEY(request_id) REFERENCES transaction_edit_requests(id) ON DELETE CASCADE,
                  FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
        )

    admin_id = None
    # Backfill: ensure every existing project has at least one member (admin).
    if _table_exists("project_members") and _table_exists("projects") and _table_exists("users"):
        admin_id = db.session.execute(
            text("SELECT id FROM users WHERE username='admin' LIMIT 1")
        ).scalar()
        if admin_id is not None:
            db.session.execute(
                text(
                    """
                    INSERT INTO project_members (project_id, user_id, created_at)
                    SELECT p.id, :admin_id, CURRENT_TIMESTAMP
                    FROM projects p
                    WHERE NOT EXISTS (
                      SELECT 1 FROM project_members pm WHERE pm.project_id=p.id
                    )
                    """
                ),
                {"admin_id": int(admin_id)},
            )

    # Projects: leader + planned dates
    if _table_exists("projects"):
        if not _column_exists("projects", "leader_user_id"):
            db.session.execute(
                text("ALTER TABLE projects ADD COLUMN leader_user_id INTEGER REFERENCES users(id)")
            )
        if not _column_exists("projects", "planned_start_date"):
            db.session.execute(text("ALTER TABLE projects ADD COLUMN planned_start_date DATE"))
        if not _column_exists("projects", "planned_end_date"):
            db.session.execute(text("ALTER TABLE projects ADD COLUMN planned_end_date DATE"))

        # Backfill planned dates for legacy rows
        db.session.execute(
            text(
                """
                UPDATE projects
                SET planned_start_date = DATE(created_at)
                WHERE planned_start_date IS NULL
                """
            )
        )
        db.session.execute(
            text(
                """
                UPDATE projects
                SET planned_end_date = DATE(created_at, '+30 day')
                WHERE planned_end_date IS NULL
                """
            )
        )

        # Backfill leader to admin if missing
        if admin_id is not None:
            db.session.execute(
                text(
                    """
                    UPDATE projects
                    SET leader_user_id = :admin_id
                    WHERE leader_user_id IS NULL
                    """
                ),
                {"admin_id": int(admin_id)},
            )

    # Project end-date change workflow
    if not _table_exists("project_end_date_change_requests"):
        db.session.execute(
            text(
                """
                CREATE TABLE project_end_date_change_requests (
                  id INTEGER PRIMARY KEY,
                  project_id INTEGER NOT NULL,
                  status VARCHAR(16) NOT NULL DEFAULT 'open',
                  old_end_date DATE NOT NULL,
                  new_end_date DATE NOT NULL,
                  created_by_user_id INTEGER,
                  created_at DATETIME NOT NULL,
                  executed_at DATETIME,
                  executed_by_user_id INTEGER,
                  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                  FOREIGN KEY(created_by_user_id) REFERENCES users(id),
                  FOREIGN KEY(executed_by_user_id) REFERENCES users(id)
                )
                """
            )
        )
        db.session.execute(
            text(
                "CREATE INDEX ix_project_end_date_change_requests_project_id "
                "ON project_end_date_change_requests (project_id)"
            )
        )

    if not _table_exists("project_end_date_change_approvals"):
        db.session.execute(
            text(
                """
                CREATE TABLE project_end_date_change_approvals (
                  request_id INTEGER NOT NULL,
                  user_id INTEGER NOT NULL,
                  approved_at DATETIME NOT NULL,
                  PRIMARY KEY (request_id, user_id),
                  FOREIGN KEY(request_id) REFERENCES project_end_date_change_requests(id) ON DELETE CASCADE,
                  FOREIGN KEY(user_id) REFERENCES users(id)
                )
                """
            )
        )

    # Project updates (progress) + attachments
    if not _table_exists("project_updates"):
        db.session.execute(
            text(
                """
                CREATE TABLE project_updates (
                  id INTEGER PRIMARY KEY,
                  project_id INTEGER NOT NULL,
                  body TEXT NOT NULL,
                  created_by_user_id INTEGER,
                  created_at DATETIME NOT NULL,
                  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                  FOREIGN KEY(created_by_user_id) REFERENCES users(id)
                )
                """
            )
        )
        db.session.execute(text("CREATE INDEX ix_project_updates_project_id ON project_updates (project_id)"))

    if not _table_exists("project_update_attachments"):
        db.session.execute(
            text(
                """
                CREATE TABLE project_update_attachments (
                  id INTEGER PRIMARY KEY,
                  update_id INTEGER NOT NULL,
                  filename VARCHAR(256) NOT NULL,
                  stored_path VARCHAR(512) NOT NULL,
                  sha256 VARCHAR(64) NOT NULL,
                  uploaded_at DATETIME NOT NULL,
                  uploaded_by_user_id INTEGER,
                  FOREIGN KEY(update_id) REFERENCES project_updates(id) ON DELETE CASCADE,
                  FOREIGN KEY(uploaded_by_user_id) REFERENCES users(id)
                )
                """
            )
        )
        db.session.execute(
            text(
                "CREATE INDEX ix_project_update_attachments_update_id "
                "ON project_update_attachments (update_id)"
            )
        )

    if not _table_exists("project_dividend_distributions"):
        db.session.execute(
            text(
                """
                CREATE TABLE project_dividend_distributions (
                  id INTEGER PRIMARY KEY,
                  transaction_id INTEGER,
                  project_id INTEGER NOT NULL,
                  recipient_user_id INTEGER,
                  recipient_name VARCHAR(128),
                  amount_cents BIGINT NOT NULL,
                  note TEXT,
                  created_at DATETIME NOT NULL,
                  created_by_user_id INTEGER,
                  FOREIGN KEY(transaction_id) REFERENCES transactions(id) ON DELETE CASCADE,
                  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                  FOREIGN KEY(recipient_user_id) REFERENCES users(id),
                  FOREIGN KEY(created_by_user_id) REFERENCES users(id)
                )
                """
            )
        )
        db.session.execute(
            text(
                "CREATE INDEX ix_project_dividend_distributions_project_id "
                "ON project_dividend_distributions (project_id)"
            )
        )
        db.session.execute(
            text(
                "CREATE INDEX ix_project_dividend_distributions_transaction_id "
                "ON project_dividend_distributions (transaction_id)"
            )
        )
        db.session.execute(
            text(
                "CREATE INDEX ix_project_dividend_distributions_recipient_user_id "
                "ON project_dividend_distributions (recipient_user_id)"
            )
        )
    else:
        # add columns if table already exists
        if not _column_exists("project_dividend_distributions", "transaction_id"):
            db.session.execute(
                text("ALTER TABLE project_dividend_distributions ADD COLUMN transaction_id INTEGER")
            )
        if not _column_exists("project_dividend_distributions", "recipient_user_id"):
            db.session.execute(
                text("ALTER TABLE project_dividend_distributions ADD COLUMN recipient_user_id INTEGER")
            )
        if not _column_exists("project_dividend_distributions", "recipient_name"):
            db.session.execute(
                text("ALTER TABLE project_dividend_distributions ADD COLUMN recipient_name VARCHAR(128)")
            )

    if not _table_exists("project_dividend_recipients"):
        db.session.execute(
            text(
                """
                CREATE TABLE project_dividend_recipients (
                  project_id INTEGER NOT NULL,
                  name VARCHAR(128) NOT NULL,
                  created_at DATETIME NOT NULL,
                  created_by_user_id INTEGER,
                  PRIMARY KEY (project_id, name),
                  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                  FOREIGN KEY(created_by_user_id) REFERENCES users(id)
                )
                """
            )
        )
        db.session.execute(
            text(
                "CREATE INDEX ix_project_dividend_recipients_project_id "
                "ON project_dividend_recipients (project_id)"
            )
        )

    if not _table_exists("project_activity_logs"):
        db.session.execute(
            text(
                """
                CREATE TABLE project_activity_logs (
                  id INTEGER PRIMARY KEY,
                  project_id INTEGER,
                  action VARCHAR(64) NOT NULL,
                  summary VARCHAR(512) NOT NULL,
                  detail TEXT,
                  actor_user_id INTEGER,
                  created_at DATETIME NOT NULL,
                  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL,
                  FOREIGN KEY(actor_user_id) REFERENCES users(id)
                )
                """
            )
        )
        db.session.execute(
            text(
                "CREATE INDEX ix_project_activity_logs_project_id "
                "ON project_activity_logs (project_id)"
            )
        )
        db.session.execute(
            text(
                "CREATE INDEX ix_project_activity_logs_created "
                "ON project_activity_logs (project_id, created_at)"
            )
        )

    db.session.commit()

