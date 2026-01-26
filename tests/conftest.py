from __future__ import annotations

import os
import sys

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from website import create_app
from website.models import create_admin_if_missing, db


@pytest.fixture()
def app(tmp_path, monkeypatch):
    uploads_path = tmp_path / "uploads"
    db_url = os.environ.get("DATABASE_URL_TEST") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL_TEST or DATABASE_URL must be set for tests.")
    if not db_url.startswith("postgresql"):
        raise RuntimeError("Tests require a PostgreSQL DATABASE_URL.")
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("UPLOAD_FOLDER", str(uploads_path))

    app = create_app(init_db=False)
    app.config["TESTING"] = True

    with app.app_context():
        db.create_all()
        create_admin_if_missing()

    yield app

    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()
