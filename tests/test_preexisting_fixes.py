"""Fixes for four issues found while reviewing the Supplax integration package
(2026-11-27), all pre-existing on main 745fc55:

1. A revoked user kept a working browser session until the cookie expired:
   load_user ignored is_approved.
2. /automations?status=<unknown> raised ValueError in Status(...) -> HTTP 500.
3. The admin edit / new / GitHub-import forms let any user (a Viewer, an
   unapproved account) become an automation's owner; /transfer already refused.
4. api_key values were stored in plaintext; now only hash_api_key(key) is kept,
   legacy plaintext rows are upgraded on first use.
"""
import re

from src.extensions import db
from src.models import Automation, Role, Status, User, hash_api_key


def _csrf_token(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "expected a csrf_token hidden field in this page"
    return m.group(1)


def _make_user(email, name, role=Role.AUTOMATOR, is_approved=True, password="pw12345"):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=is_approved)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, email, password="pw12345"):
    token = _csrf_token(client.get("/login").get_data(as_text=True))
    client.post("/login", data={"email": email, "password": password, "csrf_token": token})


class TestRevokedSessionEnds:
    def test_session_dies_on_the_next_request_after_revoke(self, app, client):
        from src.telegram_bot import handle_command
        with app.app_context():
            _make_user("a@x.com", "A")
        _login(client, "a@x.com")
        assert client.get("/automations").status_code == 200
        # The fixture's app context is the one the test client's requests run in,
        # so revoke through the same session (prod gets a fresh session per request).
        handle_command("/revoke a@x.com", lambda t: None)
        # Same reason: flask-login caches the loaded user in `g` of that shared
        # context. In prod every request has its own context, so drop the cache.
        from flask import g
        g.pop("_login_user", None)
        resp = client.get("/automations")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_approved_session_keeps_working(self, app, client):
        with app.app_context():
            _make_user("a@x.com", "A")
        _login(client, "a@x.com")
        assert client.get("/automations").status_code == 200
        assert client.get("/value").status_code == 200


class TestRegistryBadStatus:
    def test_unknown_status_is_ignored_not_500(self, app, client):
        with app.app_context():
            owner = _make_user("a@x.com", "A")
            db.session.add(Automation(slug="thing", name="Thing ТЕСТ", owner_id=owner.id))
            db.session.commit()
        _login(client, "a@x.com")
        resp = client.get("/automations?status=nonsense")
        assert resp.status_code == 200
        assert "Thing ТЕСТ" in resp.get_data(as_text=True)

    def test_known_status_still_filters(self, app, client):
        with app.app_context():
            owner = _make_user("a@x.com", "A")
            db.session.add(Automation(slug="thing", name="Thing ТЕСТ", owner_id=owner.id))
            db.session.commit()
        _login(client, "a@x.com")
        resp = client.get("/automations?status=archived")
        assert resp.status_code == 200
        assert "Thing ТЕСТ" not in resp.get_data(as_text=True)


class TestOwnerMustBeEligible:
    def _setup(self, app):
        with app.app_context():
            admin = _make_user("admin@x.com", "Admin", role=Role.ADMIN)
            viewer = _make_user("viewer@x.com", "Plain Viewer", role=Role.VIEWER)
            pending = _make_user("pending@x.com", "Pending Automator", is_approved=False)
            automator = _make_user("auto@x.com", "Real Automator")
            a = Automation(slug="thing", name="Thing", owner_id=admin.id, status=Status.IDEA)
            db.session.add(a)
            db.session.commit()
            return admin.id, viewer.id, pending.id, automator.id

    def _edit(self, client, owner_id):
        token = _csrf_token(client.get("/automations/thing/edit").get_data(as_text=True))
        return client.post("/automations/thing/edit", data={
            "name": "Thing", "status": "idea", "owner_id": str(owner_id), "csrf_token": token})

    def test_edit_rejects_a_viewer_as_owner(self, app, client):
        admin_id, viewer_id, _, _ = self._setup(app)
        _login(client, "admin@x.com")
        resp = self._edit(client, viewer_id)
        assert resp.status_code == 200  # form re-rendered with the error
        assert "Власником може бути лише Automator або Admin з доступом." in resp.get_data(as_text=True)
        with app.app_context():
            assert Automation.query.filter_by(slug="thing").first().owner_id == admin_id

    def test_edit_rejects_an_unapproved_automator(self, app, client):
        admin_id, _, pending_id, _ = self._setup(app)
        _login(client, "admin@x.com")
        self._edit(client, pending_id)
        with app.app_context():
            assert Automation.query.filter_by(slug="thing").first().owner_id == admin_id

    def test_edit_accepts_an_approved_automator(self, app, client):
        _, _, _, automator_id = self._setup(app)
        _login(client, "admin@x.com")
        resp = self._edit(client, automator_id)
        assert resp.status_code == 302
        with app.app_context():
            assert Automation.query.filter_by(slug="thing").first().owner_id == automator_id

    def test_edit_keeps_a_current_owner_who_lost_access(self, app, client):
        """Saving an unrelated field must not fail just because the current owner
        was revoked meanwhile - the owner is not being changed."""
        admin_id, _, _, automator_id = self._setup(app)
        with app.app_context():
            a = Automation.query.filter_by(slug="thing").first()
            a.owner_id = automator_id
            db.session.get(User, automator_id).is_approved = False
            db.session.commit()
        _login(client, "admin@x.com")
        resp = self._edit(client, automator_id)
        assert resp.status_code == 302

    def test_owner_dropdown_lists_only_eligible_people(self, app, client):
        self._setup(app)
        _login(client, "admin@x.com")
        html = client.get("/automations/new").get_data(as_text=True)
        assert "Real Automator" in html
        assert "Plain Viewer" not in html
        assert "Pending Automator" not in html

    def test_transfer_rejects_an_unapproved_automator(self, app, client):
        admin_id, _, pending_id, _ = self._setup(app)
        _login(client, "admin@x.com")
        token = _csrf_token(client.get("/automations/thing").get_data(as_text=True))
        client.post("/automations/thing/transfer", data={"owner_id": str(pending_id), "csrf_token": token})
        with app.app_context():
            assert Automation.query.filter_by(slug="thing").first().owner_id == admin_id


class TestApiKeyHashed:
    def test_new_user_row_never_holds_a_plaintext_key(self, app):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            assert user.api_key.startswith("sha256$")
            assert len(user.api_key) <= 64

    def test_issued_key_works_and_only_its_hash_is_stored(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            key = user.issue_api_key()
            db.session.commit()
            assert user.api_key == hash_api_key(key)
            assert key not in user.api_key
        resp = client.post("/api/automations/some-slug/sync", json={"name": "X"}, headers={"X-API-Key": key})
        assert resp.status_code == 200

    def test_the_stored_hash_itself_is_not_a_key(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            user.issue_api_key()
            db.session.commit()
            stored = user.api_key
        resp = client.post("/api/automations/some-slug/sync", json={"name": "X"}, headers={"X-API-Key": stored})
        assert resp.status_code == 401

    def test_a_legacy_plaintext_key_still_works_once_and_is_upgraded(self, app, client):
        legacy = "ab" * 32
        with app.app_context():
            user = _make_user("a@x.com", "A")
            user.api_key = legacy  # a row written before keys were hashed
            db.session.commit()
            user_id = user.id
        resp = client.post("/api/automations/some-slug/sync", json={"name": "X"}, headers={"X-API-Key": legacy})
        assert resp.status_code == 200
        with app.app_context():
            assert db.session.get(User, user_id).api_key == hash_api_key(legacy)
        # still valid after the upgrade (now matched by hash)
        resp = client.post("/api/automations/some-slug/sync", json={"name": "X"}, headers={"X-API-Key": legacy})
        assert resp.status_code == 200

    def test_regenerate_flashes_a_key_that_works(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            user_id = user.id
        _login(client, "a@x.com")
        ptoken = _csrf_token(client.get(f"/automators/{user_id}").get_data(as_text=True))
        resp = client.post(f"/automators/{user_id}/regenerate-api-key", data={"csrf_token": ptoken},
                           follow_redirects=True)
        keys = re.findall(r"\b[0-9a-f]{64}\b", resp.get_data(as_text=True))
        assert keys, "the new key must be shown once"
        with app.app_context():
            assert db.session.get(User, user_id).api_key == hash_api_key(keys[0])
        resp = client.post("/api/automations/some-slug/sync", json={"name": "X"}, headers={"X-API-Key": keys[0]})
        assert resp.status_code == 200

    def test_migrate_command_hashes_legacy_rows_and_is_idempotent(self, app):
        legacy = "cd" * 32
        with app.app_context():
            user = _make_user("a@x.com", "A")
            user.api_key = legacy
            db.session.commit()
            user_id = user.id
        runner = app.test_cli_runner()
        assert "Hashed 1" in runner.invoke(args=["migrate-hash-api-keys"]).output
        assert "Already migrated" in runner.invoke(args=["migrate-hash-api-keys"]).output
        with app.app_context():
            assert db.session.get(User, user_id).api_key == hash_api_key(legacy)
