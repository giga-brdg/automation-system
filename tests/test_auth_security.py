"""Covers the auth-hardening pass before external self-registration: CSRF
protection, /confirm brute-force lockout, api_key rotation on grant/revoke,
the automator_profile IDOR fix, and api_sync_automation's is_approved check.
See SECURITY.md's Known Limitations section (now marked resolved for these)
for the vulnerabilities each test guards against."""
import re

from src.extensions import db
from src.models import Role, User
from src import telegram


def _csrf_token(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "expected a csrf_token hidden field in this page"
    return m.group(1)


def _make_user(email, name, role=Role.VIEWER, is_confirmed=True, is_approved=True, password="pw12345"):
    user = User(email=email, name=name, role=role, is_confirmed=is_confirmed, is_approved=is_approved)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


class TestCSRF:
    def test_blocks_a_post_with_no_token(self, app, client):
        with app.app_context():
            _make_user("admin@x.com", "Admin", role=Role.ADMIN)
        client.post("/login", data={"email": "admin@x.com", "password": "pw12345"})
        # A CSRF rejection is handled by our own error handler (flash + redirect),
        # so the login must NOT have actually taken effect.
        resp = client.get("/automations")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_a_real_token_lets_login_through(self, app, client):
        with app.app_context():
            _make_user("admin@x.com", "Admin", role=Role.ADMIN)
        token = _csrf_token(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "admin@x.com", "password": "pw12345", "csrf_token": token})
        resp = client.get("/automations")
        assert resp.status_code == 200


class TestConfirmBruteForce:
    def test_locks_out_after_five_wrong_codes(self, app, client, monkeypatch):
        monkeypatch.setattr(telegram, "send_message", lambda *a, **k: True)
        token = _csrf_token(client.get("/register").get_data(as_text=True))
        client.post("/register", data={
            "name": "New Guy", "email": "new@x.com", "password": "pw12345",
            "password_confirm": "pw12345", "csrf_token": token,
        })

        confirm_page = client.get("/confirm", query_string={"email": "new@x.com"}).get_data(as_text=True)
        ctoken = _csrf_token(confirm_page)
        for _ in range(5):
            client.post("/confirm", data={"email": "new@x.com", "code": "000000", "csrf_token": ctoken})

        with app.app_context():
            user = User.query.filter_by(email="new@x.com").first()
            assert user.pending_code is None, "code should be invalidated after 5 wrong attempts"
            assert user.pending_code_attempts == 5

    def test_a_fresh_registration_resets_the_counter(self, app, client, monkeypatch):
        monkeypatch.setattr(telegram, "send_message", lambda *a, **k: True)
        with app.app_context():
            user = _make_user("new@x.com", "New Guy", is_confirmed=False, is_approved=False)
            user.pending_code = "111111"
            user.pending_code_attempts = 4
            db.session.commit()

        token = _csrf_token(client.get("/register").get_data(as_text=True))
        client.post("/register", data={
            "name": "New Guy", "email": "new@x.com", "password": "pw12345",
            "password_confirm": "pw12345", "csrf_token": token,
        })
        with app.app_context():
            user = User.query.filter_by(email="new@x.com").first()
            assert user.pending_code_attempts == 0
            real_code = user.pending_code

        ctoken = _csrf_token(client.get("/confirm", query_string={"email": "new@x.com"}).get_data(as_text=True))
        client.post("/confirm", data={"email": "new@x.com", "code": real_code, "csrf_token": ctoken})
        with app.app_context():
            user = User.query.filter_by(email="new@x.com").first()
            assert user.is_confirmed
            assert user.pending_code_attempts == 0


class TestApiKeyRotation:
    def test_grant_rotates_the_key_and_relays_it_for_automator(self, app):
        from src.telegram_bot import handle_command
        with app.app_context():
            user = _make_user("a@x.com", "A", role=Role.VIEWER, is_approved=False)
            old_key = user.api_key
            replies = []
            handle_command("/grant a@x.com automator", lambda t: replies.append(t))
            db.session.refresh(user)
            assert user.role == Role.AUTOMATOR
            assert user.is_approved
            assert user.api_key != old_key
            assert any(user.api_key in r for r in replies), "new key must be relayed for automator/admin"

    def test_grant_does_not_relay_a_key_for_plain_viewer(self, app):
        from src.telegram_bot import handle_command
        with app.app_context():
            _make_user("v@x.com", "V", role=Role.VIEWER, is_approved=False)
            replies = []
            handle_command("/grant v@x.com viewer", lambda t: replies.append(t))
            assert not any("API-ключ" in r for r in replies)

    def test_revoke_rotates_the_key(self, app):
        from src.telegram_bot import handle_command
        with app.app_context():
            user = _make_user("a@x.com", "A", role=Role.AUTOMATOR, is_approved=True)
            old_key = user.api_key
            handle_command("/revoke a@x.com", lambda t: None)
            db.session.refresh(user)
            assert not user.is_approved
            assert user.api_key != old_key

    def test_self_service_regenerate_rejects_other_users(self, app, client):
        with app.app_context():
            u1 = _make_user("a@x.com", "A", role=Role.AUTOMATOR)
            u2 = _make_user("b@x.com", "B", role=Role.AUTOMATOR)
            u1_id, u2_id = u1.id, u2.id
        token = _csrf_token(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "b@x.com", "password": "pw12345", "csrf_token": token})
        ptoken = _csrf_token(client.get(f"/automators/{u2_id}").get_data(as_text=True))
        resp = client.post(f"/automators/{u1_id}/regenerate-api-key", data={"csrf_token": ptoken})
        assert resp.status_code == 403

    def test_self_service_regenerate_rotates_own_key(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A", role=Role.AUTOMATOR)
            user_id = user.id
            old_key = user.api_key
        token = _csrf_token(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "a@x.com", "password": "pw12345", "csrf_token": token})
        ptoken = _csrf_token(client.get(f"/automators/{user_id}").get_data(as_text=True))
        client.post(f"/automators/{user_id}/regenerate-api-key", data={"csrf_token": ptoken})
        with app.app_context():
            assert db.session.get(User, user_id).api_key != old_key


class TestAutomatorProfileIDOR:
    def test_a_viewer_cannot_see_another_users_email(self, app, client):
        with app.app_context():
            other = _make_user("secret@x.com", "Secret Owner", role=Role.AUTOMATOR)
            other_id = other.id
            _make_user("viewer@x.com", "Plain Viewer", role=Role.VIEWER)
        token = _csrf_token(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "viewer@x.com", "password": "pw12345", "csrf_token": token})

        html = client.get(f"/automators/{other_id}").get_data(as_text=True)
        assert "secret@x.com" not in html
        assert "Secret Owner" in html  # the page itself still has to work

    def test_admin_can_see_the_email(self, app, client):
        with app.app_context():
            other = _make_user("secret@x.com", "Secret Owner", role=Role.AUTOMATOR)
            other_id = other.id
            _make_user("admin@x.com", "Admin", role=Role.ADMIN)
        token = _csrf_token(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "admin@x.com", "password": "pw12345", "csrf_token": token})
        html = client.get(f"/automators/{other_id}").get_data(as_text=True)
        assert "secret@x.com" in html

    def test_a_user_can_see_their_own_email(self, app, client):
        with app.app_context():
            me = _make_user("me@x.com", "Me", role=Role.VIEWER)
            me_id = me.id
        token = _csrf_token(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "me@x.com", "password": "pw12345", "csrf_token": token})
        html = client.get(f"/automators/{me_id}").get_data(as_text=True)
        assert "me@x.com" in html


class TestApiSyncApproval:
    def test_rejects_a_role_matching_but_unapproved_account(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A", role=Role.AUTOMATOR, is_approved=False)
            key = user.api_key
        resp = client.post("/api/automations/some-slug/sync", json={"name": "X"},
                            headers={"X-API-Key": key})
        assert resp.status_code == 403

    def test_accepts_an_approved_automator(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A", role=Role.AUTOMATOR, is_approved=True)
            key = user.api_key
        resp = client.post("/api/automations/some-slug/sync", json={"name": "X"},
                            headers={"X-API-Key": key})
        assert resp.status_code == 200

    def test_is_exempt_from_csrf(self, app, client):
        """No csrf_token anywhere in this request - it must not be rejected
        for that reason (401/403 for auth are fine, a CSRF-triggered
        redirect/400 would not be)."""
        with app.app_context():
            user = _make_user("a@x.com", "A", role=Role.AUTOMATOR, is_approved=True)
            key = user.api_key
        resp = client.post("/api/automations/some-slug/sync", json={"name": "X"},
                            headers={"X-API-Key": key})
        assert resp.status_code == 200


class TestSessionCookieFlags:
    def test_httponly_and_samesite_always_set(self, app):
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_secure_follows_railway_environment(self, monkeypatch):
        import importlib
        from src import app as app_module

        monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
        local_app = app_module.create_app()
        assert local_app.config["SESSION_COOKIE_SECURE"] is False

        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        prod_app = app_module.create_app()
        assert prod_app.config["SESSION_COOKIE_SECURE"] is True


class TestLoginRateLimit:
    def test_blocks_after_ten_attempts_per_minute(self, app, client):
        with app.app_context():
            _make_user("admin@x.com", "Admin", role=Role.ADMIN)
        token = _csrf_token(client.get("/login").get_data(as_text=True))

        statuses = []
        for _ in range(11):
            resp = client.post("/login", data={
                "email": "admin@x.com", "password": "wrong-password", "csrf_token": token,
            })
            statuses.append(resp.status_code)

        assert statuses[:10] == [200] * 10, "the first 10 attempts should each render normally (wrong password)"
        assert statuses[10] == 429, "the 11th attempt within the window should be rate-limited"

    def test_get_requests_are_never_rate_limited(self, app, client):
        # Only POST counts against the limit - repeatedly loading the empty
        # login form must never itself trigger a 429.
        for _ in range(15):
            resp = client.get("/login")
            assert resp.status_code == 200

    def test_a_real_client_ip_is_used_behind_the_proxy(self, app, monkeypatch):
        """Without ProxyFix, every request behind Railway's edge would look
        like it came from the same address (the proxy's own IP) - this
        confirms X-Forwarded-For is actually trusted once RAILWAY_ENVIRONMENT
        is set, so two different clients get two different rate-limit
        buckets instead of sharing one. Depends on the `app` fixture (even
        though it builds its own second app below) purely for the temp-file
        DATABASE_URL it sets - without it, create_app() would default to the
        real local data/portfolio.db."""
        from src import app as app_module
        from src.app import limiter
        from src.extensions import db as prod_db

        monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
        prod_app = app_module.create_app()
        limiter.storage.reset()
        with prod_app.app_context():
            prod_db.create_all()
            _make_user("admin2@x.com", "Admin2", role=Role.ADMIN)

        client = prod_app.test_client()
        token = _csrf_token(client.get("/login").get_data(as_text=True))

        # Exhaust the limit as client A.
        for _ in range(10):
            client.post("/login", data={"email": "admin2@x.com", "password": "wrong", "csrf_token": token},
                        headers={"X-Forwarded-For": "203.0.113.10"})
        blocked = client.post("/login", data={"email": "admin2@x.com", "password": "wrong", "csrf_token": token},
                               headers={"X-Forwarded-For": "203.0.113.10"})
        assert blocked.status_code == 429

        # A different client (different forwarded IP) must not be blocked.
        fresh = client.post("/login", data={"email": "admin2@x.com", "password": "wrong", "csrf_token": token},
                             headers={"X-Forwarded-For": "203.0.113.99"})
        assert fresh.status_code == 200

        # This test's own second engine, same temp DB file as the `app`
        # fixture - dispose it here or the fixture's teardown fails to
        # unlink that file on Windows (same reason conftest.py disposes
        # its own engine before unlinking).
        with prod_app.app_context():
            prod_db.engine.dispose()
