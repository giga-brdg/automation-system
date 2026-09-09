"""Shared pytest fixtures. See TESTING.md's Integration section for why
DATABASE_URL has to be set *before* create_app() is called - create_app()
reads it from the environment at call time, defaulting to the real
data/portfolio.db file when unset."""
import os
import tempfile

import pytest


@pytest.fixture
def app():
    """A fresh app per test, backed by its own temp-file SQLite database -
    not :memory:, since a plain SQLAlchemy engine opens a new connection per
    checkout and :memory: doesn't survive that across requests without extra
    pooling config. Torn down (file deleted) after the test regardless of
    outcome."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    old_db_url = os.environ.get("DATABASE_URL")
    old_auth_secret = os.environ.get("AUTH_SECRET")
    os.environ["DATABASE_URL"] = "sqlite:///" + db_path.replace("\\", "/")
    os.environ["AUTH_SECRET"] = "test-only-secret-do-not-use-in-prod"

    from src.app import create_app
    from src.extensions import db

    flask_app = create_app()
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        # Windows keeps an open file handle through SQLAlchemy's engine pool
        # even after the session is closed - unlink below fails with
        # PermissionError unless the engine itself is disposed first.
        db.engine.dispose()

    if old_db_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = old_db_url
    if old_auth_secret is None:
        os.environ.pop("AUTH_SECRET", None)
    else:
        os.environ["AUTH_SECRET"] = old_auth_secret
    os.unlink(db_path)


@pytest.fixture
def client(app):
    return app.test_client()
