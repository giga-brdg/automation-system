"""Covers /api/security-scan/trigger - the machine-facing twin of the
"Запустити перевірку" button (automation_trigger_security_scan), for a
CLI/skill (security-alert-fix) to queue a rescan of one repo using this
app's own SECURITY_SCAN_TRIGGER_TOKEN, without the calling automator needing
personal GitHub access to giga-brdg/github-security-scan. Same X-API-Key
auth shape as api_sync_automation (see test_auth_security.py's
TestApiSyncApproval), plus a can_manage check on whichever automation is
registered for the requested repo."""
from src import github_sync
from src.extensions import db
from src.models import Automation, Role, User


def _make_user(email, name, role=Role.AUTOMATOR, is_approved=True, password="pw12345"):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=is_approved)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


class TestApiTriggerSecurityScan:
    def test_dispatches_scan_for_the_matched_automation(self, app, client, monkeypatch):
        monkeypatch.setenv("SECURITY_SCAN_TRIGGER_TOKEN", "trigger-tok")
        calls = []
        monkeypatch.setattr(github_sync, "dispatch_security_scan",
                             lambda owner, repo, tok: calls.append((owner, repo, tok)))
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     repo_url="https://github.com/acme/thing")
            db.session.add(automation)
            db.session.commit()
            key = user.api_key

        resp = client.post("/api/security-scan/trigger", json={"repo": "acme/thing"},
                            headers={"X-API-Key": key})
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert calls == [("acme", "thing", "trigger-tok")]

    def test_missing_api_key_is_unauthorized(self, app, client):
        resp = client.post("/api/security-scan/trigger", json={"repo": "acme/thing"})
        assert resp.status_code == 401

    def test_unapproved_account_is_forbidden(self, app, client, monkeypatch):
        monkeypatch.setenv("SECURITY_SCAN_TRIGGER_TOKEN", "trigger-tok")
        with app.app_context():
            user = _make_user("a@x.com", "A", is_approved=False)
            key = user.api_key
        resp = client.post("/api/security-scan/trigger", json={"repo": "acme/thing"},
                            headers={"X-API-Key": key})
        assert resp.status_code == 403

    def test_viewer_role_is_forbidden(self, app, client, monkeypatch):
        monkeypatch.setenv("SECURITY_SCAN_TRIGGER_TOKEN", "trigger-tok")
        with app.app_context():
            user = _make_user("a@x.com", "A", role=Role.VIEWER)
            key = user.api_key
        resp = client.post("/api/security-scan/trigger", json={"repo": "acme/thing"},
                            headers={"X-API-Key": key})
        assert resp.status_code == 403

    def test_cannot_trigger_a_scan_for_someone_elses_automation(self, app, client, monkeypatch):
        """A valid, approved key alone isn't enough - the caller must also
        manage this specific automation (IDOR guard, same shape as
        api_sync_automation's owner check)."""
        monkeypatch.setenv("SECURITY_SCAN_TRIGGER_TOKEN", "trigger-tok")
        calls = []
        monkeypatch.setattr(github_sync, "dispatch_security_scan",
                             lambda owner, repo, tok: calls.append((owner, repo, tok)))
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
            other = _make_user("other@x.com", "Other")
            automation = Automation(slug="thing", name="Thing", owner_id=owner.id,
                                     repo_url="https://github.com/acme/thing")
            db.session.add(automation)
            db.session.commit()
            other_key = other.api_key

        resp = client.post("/api/security-scan/trigger", json={"repo": "acme/thing"},
                            headers={"X-API-Key": other_key})
        assert resp.status_code == 403
        assert calls == []

    def test_admin_can_trigger_any_automations_scan(self, app, client, monkeypatch):
        monkeypatch.setenv("SECURITY_SCAN_TRIGGER_TOKEN", "trigger-tok")
        calls = []
        monkeypatch.setattr(github_sync, "dispatch_security_scan",
                             lambda owner, repo, tok: calls.append((owner, repo, tok)))
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
            admin = _make_user("admin@x.com", "Admin", role=Role.ADMIN)
            automation = Automation(slug="thing", name="Thing", owner_id=owner.id,
                                     repo_url="https://github.com/acme/thing")
            db.session.add(automation)
            db.session.commit()
            admin_key = admin.api_key

        resp = client.post("/api/security-scan/trigger", json={"repo": "acme/thing"},
                            headers={"X-API-Key": admin_key})
        assert resp.status_code == 200
        assert calls == [("acme", "thing", "trigger-tok")]

    def test_unknown_repo_is_not_found(self, app, client, monkeypatch):
        monkeypatch.setenv("SECURITY_SCAN_TRIGGER_TOKEN", "trigger-tok")
        with app.app_context():
            user = _make_user("a@x.com", "A")
            key = user.api_key
        resp = client.post("/api/security-scan/trigger", json={"repo": "acme/nope"},
                            headers={"X-API-Key": key})
        assert resp.status_code == 404

    def test_malformed_repo_is_a_bad_request(self, app, client, monkeypatch):
        monkeypatch.setenv("SECURITY_SCAN_TRIGGER_TOKEN", "trigger-tok")
        with app.app_context():
            user = _make_user("a@x.com", "A")
            key = user.api_key
        resp = client.post("/api/security-scan/trigger", json={"repo": "not-a-repo"},
                            headers={"X-API-Key": key})
        assert resp.status_code == 400

    def test_dispatch_failure_is_a_502_not_a_500(self, app, client, monkeypatch):
        monkeypatch.setenv("SECURITY_SCAN_TRIGGER_TOKEN", "trigger-tok")

        def boom(owner, repo, tok):
            raise RuntimeError("HTTP 401")
        monkeypatch.setattr(github_sync, "dispatch_security_scan", boom)
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     repo_url="https://github.com/acme/thing")
            db.session.add(automation)
            db.session.commit()
            key = user.api_key

        resp = client.post("/api/security-scan/trigger", json={"repo": "acme/thing"},
                            headers={"X-API-Key": key})
        assert resp.status_code == 502

    def test_is_exempt_from_csrf(self, app, client, monkeypatch):
        """No csrf_token anywhere in this request - it must not be rejected
        for that reason (401/403/404 for auth/lookup are fine, a
        CSRF-triggered redirect/400 would not be)."""
        monkeypatch.setenv("SECURITY_SCAN_TRIGGER_TOKEN", "trigger-tok")
        with app.app_context():
            user = _make_user("a@x.com", "A")
            key = user.api_key
        resp = client.post("/api/security-scan/trigger", json={"repo": "acme/thing"},
                            headers={"X-API-Key": key})
        assert resp.status_code == 404  # reached the lookup, not a CSRF 302/400
