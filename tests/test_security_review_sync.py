"""Covers dashboard/SECURITY_REVIEW.md parsing/sync and the three-state badge
(Automation.security_review_state) this feeds: "не перевірено" when nobody's
ever synced the file, "чисто" when the last run had no open High/Medium, and
"є знахідки" otherwise. See src/github_sync.py's
security_review_fields_from_sections and src/app.py's
sync_automation_from_github (fetches dashboard/SECURITY_REVIEW.md alongside
ROI.md/SUMMARY.md/backlog/BACKLOG.md)."""
import re
from datetime import datetime

from src import github_sync
from src.extensions import db
from src.models import Automation, Role, User


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


class TestSecurityReviewParsing:
    def test_parses_date_and_zero_findings(self):
        text = "## Last Review\n2026-09-14\n\n## Open Findings\n- High: 0\n- Medium: 0\n"
        fields = github_sync.security_review_fields_from_sections(
            github_sync.parse_security_review_md(text))
        assert fields["reviewed_at"] == datetime(2026, 9, 14)
        assert fields["high"] == 0
        assert fields["medium"] == 0

    def test_parses_open_findings(self):
        text = "## Last Review\n2026-09-01\n\n## Open Findings\n- High: 1\n- Medium: 3\n"
        fields = github_sync.security_review_fields_from_sections(
            github_sync.parse_security_review_md(text))
        assert fields["high"] == 1
        assert fields["medium"] == 3

    def test_missing_date_is_none(self):
        fields = github_sync.security_review_fields_from_sections(
            github_sync.parse_security_review_md("## Open Findings\n- High: 0\n"))
        assert fields["reviewed_at"] is None

    def test_malformed_date_is_none_not_a_crash(self):
        text = "## Last Review\nсьогодні\n"
        fields = github_sync.security_review_fields_from_sections(
            github_sync.parse_security_review_md(text))
        assert fields["reviewed_at"] is None

    def test_missing_findings_default_to_zero(self):
        fields = github_sync.security_review_fields_from_sections(
            github_sync.parse_security_review_md("## Last Review\n2026-09-14\n"))
        assert fields["high"] == 0
        assert fields["medium"] == 0

    def test_empty_text_is_never_reviewed(self):
        fields = github_sync.security_review_fields_from_sections(
            github_sync.parse_security_review_md(""))
        assert fields == {"reviewed_at": None, "high": 0, "medium": 0}


class TestSecurityReviewBadgeState:
    def test_never_synced_is_none_state(self, app):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            assert automation.security_review_state == "none"
            assert automation.security_review_label == "Не перевірено"

    def test_reviewed_with_zero_findings_is_clean(self, app):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     security_review_at=datetime(2026, 9, 14),
                                     security_review_high=0, security_review_medium=0)
            assert automation.security_review_state == "clean"

    def test_reviewed_with_a_high_finding(self, app):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     security_review_at=datetime(2026, 9, 14),
                                     security_review_high=1, security_review_medium=0)
            assert automation.security_review_state == "high"

    def test_reviewed_with_only_a_medium_finding(self, app):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     security_review_at=datetime(2026, 9, 14),
                                     security_review_high=0, security_review_medium=1)
            assert automation.security_review_state == "medium"

    def test_high_takes_priority_over_medium(self, app):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     security_review_at=datetime(2026, 9, 14),
                                     security_review_high=1, security_review_medium=5)
            assert automation.security_review_state == "high"

    def test_null_counts_after_a_reviewed_date_count_as_clean(self, app):
        """A row from before this column existed, or a file with a
        '## Last Review' but no '## Open Findings' at all - counts stay
        None, not 0, but should read the same as a clean review, not crash
        the (high or 0) + (medium or 0) arithmetic."""
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     security_review_at=datetime(2026, 9, 14))
            assert automation.security_review_state == "clean"


class TestSecurityReviewGithubSync:
    def _stub_fetch(self, monkeypatch, security_review_text):
        def fake_fetch(owner, repo, path, branch):
            if path == "README.md":
                return "# Thing\n\nA thing.\n"
            if path == "dashboard/SECURITY_REVIEW.md":
                return security_review_text
            return None
        monkeypatch.setattr(github_sync, "fetch_raw_file", fake_fetch)
        monkeypatch.setattr(github_sync, "default_branch", lambda owner, repo: "main")

    def test_import_picks_up_a_clean_review(self, app, client, monkeypatch):
        self._stub_fetch(monkeypatch, "## Last Review\n2026-09-14\n\n## Open Findings\n- High: 0\n- Medium: 0\n")
        with app.app_context():
            _make_user("a@x.com", "A")
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/import-github").get_data(as_text=True))
        resp = client.post("/automations/import-github", data={
            "repo_url": "https://github.com/acme/thing", "slug": "thing",
            "owner_id": "", "csrf_token": token,
        })
        assert resp.status_code == 302
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.security_review_state == "clean"
            assert automation.security_review_at.strftime("%Y-%m-%d") == "2026-09-14"

    def test_import_without_the_file_stays_not_reviewed(self, app, client, monkeypatch):
        self._stub_fetch(monkeypatch, None)
        with app.app_context():
            _make_user("a@x.com", "A")
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/import-github").get_data(as_text=True))
        client.post("/automations/import-github", data={
            "repo_url": "https://github.com/acme/thing", "slug": "thing",
            "owner_id": "", "csrf_token": token,
        })
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.security_review_state == "none"

    def test_resync_with_open_findings_shows_up_on_the_card(self, app, client, monkeypatch):
        self._stub_fetch(monkeypatch, "## Last Review\n2026-09-10\n\n## Open Findings\n- High: 2\n- Medium: 1\n")
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     repo_url="https://github.com/acme/thing")
            db.session.add(automation)
            db.session.commit()
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/thing").get_data(as_text=True))
        client.post("/automations/thing/resync", data={"csrf_token": token})
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.security_review_state == "high"
            assert automation.security_review_high == 2
            assert automation.security_review_medium == 1

        html = client.get("/automations/thing").get_data(as_text=True)
        assert "Перевірено — є знахідки (High)" in html


class TestSecurityReviewOnlyResync:
    """The dedicated 'Оновити' button on the Security review card
    (/automations/<slug>/resync-security) - re-fetches only
    dashboard/SECURITY_REVIEW.md, unlike the full 'Оновити з GitHub' button."""

    def _stub_fetch(self, monkeypatch, security_review_text, other_calls=None):
        def fake_fetch(owner, repo, path, branch):
            if other_calls is not None:
                other_calls.append(path)
            if path == "dashboard/SECURITY_REVIEW.md":
                return security_review_text
            raise AssertionError(f"resync-security should only fetch SECURITY_REVIEW.md, got {path}")
        monkeypatch.setattr(github_sync, "fetch_raw_file", fake_fetch)
        monkeypatch.setattr(github_sync, "default_branch", lambda owner, repo: "main")

    def test_updates_only_security_fields(self, app, client, monkeypatch):
        self._stub_fetch(monkeypatch, "## Last Review\n2026-09-20\n\n## Open Findings\n- High: 1\n- Medium: 0\n")
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     one_liner="original one-liner",
                                     repo_url="https://github.com/acme/thing")
            db.session.add(automation)
            db.session.commit()
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/thing").get_data(as_text=True))
        resp = client.post("/automations/thing/resync-security", data={"csrf_token": token})
        assert resp.status_code == 302
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.security_review_state == "high"
            assert automation.security_review_high == 1
            assert automation.security_review_medium == 0
            assert automation.security_review_at.strftime("%Y-%m-%d") == "2026-09-20"
            # Unrelated fields untouched - this button only refreshes security data.
            assert automation.one_liner == "original one-liner"

    def test_missing_file_flashes_error_and_keeps_previous_state(self, app, client, monkeypatch):
        self._stub_fetch(monkeypatch, None)
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     repo_url="https://github.com/acme/thing",
                                     security_review_at=datetime(2026, 9, 1),
                                     security_review_high=0, security_review_medium=0)
            db.session.add(automation)
            db.session.commit()
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/thing").get_data(as_text=True))
        client.post("/automations/thing/resync-security", data={"csrf_token": token})
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.security_review_state == "clean"
            assert automation.security_review_at.strftime("%Y-%m-%d") == "2026-09-01"

    def test_without_manage_permission_is_forbidden(self, app, client, monkeypatch):
        self._stub_fetch(monkeypatch, "## Last Review\n2026-09-20\n\n## Open Findings\n- High: 0\n- Medium: 0\n")
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
            _make_user("other@x.com", "Other", role=Role.VIEWER)
            automation = Automation(slug="thing", name="Thing", owner_id=owner.id,
                                     repo_url="https://github.com/acme/thing")
            db.session.add(automation)
            db.session.commit()
        token = _csrf_token(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "other@x.com", "password": "pw12345", "csrf_token": token})
        resp = client.post("/automations/thing/resync-security", data={"csrf_token": token})
        assert resp.status_code == 403


class TestSecurityScanTrigger:
    """The 'Запустити перевірку' button
    (/automations/<slug>/trigger-security-scan) - fires a workflow_dispatch
    on the separate github-security-scan automation via
    github_sync.dispatch_security_scan, scoped to just this repo. Doesn't
    touch security_review_* itself (the scan writes those back to GitHub
    asynchronously; 'Оновити' picks the result up later)."""

    def test_dispatches_scan_for_this_repos_owner_and_name(self, app, client, monkeypatch):
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
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/thing").get_data(as_text=True))
        resp = client.post("/automations/thing/trigger-security-scan", data={"csrf_token": token})
        assert resp.status_code == 302
        assert calls == [("acme", "thing", "trigger-tok")]

    def test_without_trigger_token_configured_flashes_error(self, app, client, monkeypatch):
        monkeypatch.delenv("SECURITY_SCAN_TRIGGER_TOKEN", raising=False)
        calls = []
        monkeypatch.setattr(github_sync, "dispatch_security_scan",
                             lambda owner, repo, tok: calls.append((owner, repo, tok)))
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     repo_url="https://github.com/acme/thing")
            db.session.add(automation)
            db.session.commit()
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/thing").get_data(as_text=True))
        resp = client.post("/automations/thing/trigger-security-scan", data={"csrf_token": token})
        assert resp.status_code == 302
        assert calls == []

    def test_without_repo_url_flashes_error(self, app, client, monkeypatch):
        monkeypatch.setenv("SECURITY_SCAN_TRIGGER_TOKEN", "trigger-tok")
        calls = []
        monkeypatch.setattr(github_sync, "dispatch_security_scan",
                             lambda owner, repo, tok: calls.append((owner, repo, tok)))
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            db.session.add(automation)
            db.session.commit()
        token = _csrf_token(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "a@x.com", "password": "pw12345", "csrf_token": token})
        client.post("/automations/thing/trigger-security-scan", data={"csrf_token": token})
        assert calls == []

    def test_dispatch_failure_flashes_error_instead_of_500(self, app, client, monkeypatch):
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
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/thing").get_data(as_text=True))
        resp = client.post("/automations/thing/trigger-security-scan", data={"csrf_token": token})
        assert resp.status_code == 302
        html = client.get("/automations/thing").get_data(as_text=True)
        assert "Не вдалося запустити сканування" in html

    def test_without_manage_permission_is_forbidden(self, app, client, monkeypatch):
        monkeypatch.setenv("SECURITY_SCAN_TRIGGER_TOKEN", "trigger-tok")
        monkeypatch.setattr(github_sync, "dispatch_security_scan", lambda owner, repo, tok: None)
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
            _make_user("other@x.com", "Other", role=Role.VIEWER)
            automation = Automation(slug="thing", name="Thing", owner_id=owner.id,
                                     repo_url="https://github.com/acme/thing")
            db.session.add(automation)
            db.session.commit()
        token = _csrf_token(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "other@x.com", "password": "pw12345", "csrf_token": token})
        resp = client.post("/automations/thing/trigger-security-scan", data={"csrf_token": token})
        assert resp.status_code == 403
