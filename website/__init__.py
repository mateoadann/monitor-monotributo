import os

from flask import Flask, flash, redirect, url_for
from flask_login import LoginManager, current_user

from website.models import create_admin_if_missing, db, seed_data


login_manager = LoginManager()
login_manager.login_view = "auth.login"


def _migrate_user_table(app):
    """Agrega columnas de roles al modelo User si no existen (idempotente)."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    columns = [col["name"] for col in inspector.get_columns("user")]

    migrations = []
    if "role" not in columns:
        migrations.append(
            "ALTER TABLE \"user\" ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'visor'"
        )
    if "is_active_user" not in columns:
        migrations.append(
            "ALTER TABLE \"user\" ADD COLUMN is_active_user BOOLEAN NOT NULL DEFAULT TRUE"
        )
    if "nombre" not in columns:
        migrations.append(
            "ALTER TABLE \"user\" ADD COLUMN nombre VARCHAR(160) NOT NULL DEFAULT ''"
        )

    if migrations:
        for sql in migrations:
            db.session.execute(text(sql))
        db.session.execute(
            text("UPDATE \"user\" SET role = 'admin' WHERE username = 'admin'")
        )
        db.session.commit()


def _migrate_factura_import_table(app):
    """Agrega batch_id e indice en factura_import si no existen (idempotente)."""
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    columns = [col["name"] for col in inspector.get_columns("factura_import")]
    indexes = [idx["name"] for idx in inspector.get_indexes("factura_import")]

    migrations = []
    if "batch_id" not in columns:
        migrations.append("ALTER TABLE factura_import ADD COLUMN batch_id VARCHAR(32)")
    if "ix_factura_import_batch_id" not in indexes:
        migrations.append(
            "CREATE INDEX IF NOT EXISTS ix_factura_import_batch_id ON factura_import (batch_id)"
        )

    if migrations:
        for sql in migrations:
            db.session.execute(text(sql))
        db.session.commit()


def create_app(init_db: bool = True):
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me")
    default_uploads = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "uploads")
    )
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL must be set.")
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["UPLOAD_FOLDER"] = os.environ.get("UPLOAD_FOLDER", default_uploads)
    app.config["REDIS_URL"] = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    from website.auth import auth_bp
    from website.views import main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    db.init_app(app)
    login_manager.init_app(app)

    from website.models import get_user_by_id

    @login_manager.user_loader
    def load_user(user_id):
        return get_user_by_id(user_id)

    @app.errorhandler(403)
    def forbidden(e):
        flash("No tienes permisos para realizar esta accion.", "error")
        return redirect(url_for("main.dashboard"))

    @app.after_request
    def set_no_cache(response):
        if current_user.is_authenticated:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    if init_db:
        with app.app_context():
            db.create_all()
            _migrate_user_table(app)
            _migrate_factura_import_table(app)
            create_admin_if_missing()
            seed_data()

    return app
