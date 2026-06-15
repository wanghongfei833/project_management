import os
from pathlib import Path

from flask import Flask
from flask_wtf import CSRFProtect
import sqlite3
from werkzeug.middleware.proxy_fix import ProxyFix

from sqlalchemy import event
from sqlalchemy.engine import Engine

from .extensions import db, login_manager
from .models import Role
from .schema import ensure_sqlite_schema
from .seed import ensure_seed_data
from .middleware import PrefixMiddleware

csrf = CSRFProtect()

_SQLITE_CONNECT_HOOK_INSTALLED = False


def create_app():
    project_root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(project_root / "templates"),
        static_folder=str(project_root / "static"),
    )

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
        "DATABASE_URL", "sqlite:///ledger.db"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["UPLOAD_FOLDER"] = os.environ.get(
        "UPLOAD_FOLDER", str(Path(app.root_path).parent / "uploads")
    )
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

    # If you deploy under a subpath (方案A), set URL_PREFIX like "/PM".
    # Nginx should also set X-Forwarded-Prefix to the same prefix.
    url_prefix = (os.environ.get("URL_PREFIX") or "").strip()
    if url_prefix and not url_prefix.startswith("/"):
        url_prefix = "/" + url_prefix
    url_prefix = url_prefix.rstrip("/")
    app.config["URL_PREFIX"] = url_prefix

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    # Respect reverse proxy headers (scheme/host) and prefix.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
    app.wsgi_app = PrefixMiddleware(app.wsgi_app)

    # Keep session cookie scoped to the prefix to avoid conflicts under same domain.
    if url_prefix:
        app.config["SESSION_COOKIE_PATH"] = url_prefix + "/"

    db.init_app(app)

    # Register SQLite pragmas AFTER init_app (db.engine requires app context)
    global _SQLITE_CONNECT_HOOK_INSTALLED
    if not _SQLITE_CONNECT_HOOK_INSTALLED:

        @event.listens_for(Engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover
            if not isinstance(dbapi_connection, sqlite3.Connection):
                return
            try:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
            except Exception:
                pass

        _SQLITE_CONNECT_HOOK_INSTALLED = True

    login_manager.init_app(app)
    csrf.init_app(app)

    from .routes import bp as main_bp

    app.register_blueprint(main_bp)

    @app.context_processor
    def inject_nav_flags():
        from flask_login import current_user

        return {
            "is_admin": getattr(current_user, "role", None) == Role.ADMIN.value,
        }

    with app.app_context():
        db.create_all()
        # Ensure existing SQLite DB gets new columns/tables
        ensure_sqlite_schema()
        ensure_seed_data()

    return app

