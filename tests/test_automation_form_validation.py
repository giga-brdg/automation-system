"""Covers validate_automation_form (src/app.py), added by the 2026-09-17
design audit fixes. Before this, /automations/new had no server-side
validation at all beyond HTML5 `required` - a duplicate slug crashed with an
unhandled IntegrityError (a UNIQUE constraint on Automation.slug), and a
non-numeric budget/multiplier string would have crashed at db.session.commit()
inside apply_manual_form's Numeric-column assignment. These tests lock in
that both now surface as a normal per-field form error instead."""
import re

from src.extensions import db
from src.models import Automation, Role, User


def _make_user(email, name, role=Role.AUTOMATOR):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=True)
    user.set_password("pw12345")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, email):
    token_html = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', token_html).group(1)
    client.post("/login", data={"email": email, "password": "pw12345", "csrf_token": token})


def _new_form_token(client):
    html = client.get("/automations/new").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


class TestAutomationFormValidation:
    def test_duplicate_slug_is_rejected_not_crashed(self, app, client):
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
            db.session.add(Automation(slug="taken-slug", name="Existing", owner=owner))
            db.session.commit()
        _login(client, "owner@x.com")
        token = _new_form_token(client)
        resp = client.post("/automations/new", data={
            "csrf_token": token, "slug": "taken-slug", "name": "New one", "status": "idea",
        })
        assert resp.status_code == 200
        assert "вже існує" in resp.get_data(as_text=True)
        with app.app_context():
            assert Automation.query.filter_by(slug="taken-slug").count() == 1

    def test_missing_name_is_rejected(self, app, client):
        with app.app_context():
            _make_user("owner@x.com", "Owner")
        _login(client, "owner@x.com")
        token = _new_form_token(client)
        resp = client.post("/automations/new", data={
            "csrf_token": token, "slug": "no-name-here", "name": "", "status": "idea",
        })
        assert resp.status_code == 200
        assert "Вкажи назву" in resp.get_data(as_text=True)
        with app.app_context():
            assert Automation.query.filter_by(slug="no-name-here").first() is None

    def test_non_numeric_budget_is_rejected_not_crashed(self, app, client):
        with app.app_context():
            _make_user("owner@x.com", "Owner")
        _login(client, "owner@x.com")
        token = _new_form_token(client)
        resp = client.post("/automations/new", data={
            "csrf_token": token, "slug": "bad-budget", "name": "Bad budget", "status": "idea",
            "monthly_token_budget_usd": "not-a-number",
        })
        assert resp.status_code == 200
        assert "введи число" in resp.get_data(as_text=True)
        with app.app_context():
            assert Automation.query.filter_by(slug="bad-budget").first() is None

    def test_javascript_url_is_dropped_not_stored(self, app, client):
        """repo_url/clickup_url/presentation_url get rendered straight into an
        <a href> (automation_detail.html) - a 'javascript:' value must never
        reach the database, or every viewer who clicks the link runs it."""
        with app.app_context():
            _make_user("owner@x.com", "Owner")
        _login(client, "owner@x.com")
        token = _new_form_token(client)
        resp = client.post("/automations/new", data={
            "csrf_token": token, "slug": "xss-try", "name": "XSS try", "status": "idea",
            "repo_url": "javascript:alert(document.cookie)",
            "clickup_url": "javascript:alert(1)",
            "presentation_url": "javascript:alert(1)",
        })
        assert resp.status_code == 302
        with app.app_context():
            automation = Automation.query.filter_by(slug="xss-try").first()
            assert automation.repo_url is None
            assert automation.clickup_url is None
            assert automation.roi.presentation_url is None

    def test_valid_submission_still_creates_and_redirects_to_stage1(self, app, client):
        with app.app_context():
            _make_user("owner@x.com", "Owner")
        _login(client, "owner@x.com")
        token = _new_form_token(client)
        resp = client.post("/automations/new", data={
            "csrf_token": token, "slug": "good-one", "name": "Good One", "status": "idea",
        })
        assert resp.status_code == 302
        assert "/stage1" in resp.headers["Location"]
        with app.app_context():
            assert Automation.query.filter_by(slug="good-one").first() is not None
