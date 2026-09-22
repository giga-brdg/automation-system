"""Covers the Stage 1 interview feature: filling it in at
/automations/<slug>/stage1, storage on Automation.stage1_answers, and the
read-only GET /api/automations/<slug>/stage1-answers endpoint stage-1-supplax
pulls from (see src/stage1_questions.py, SECURITY.md's Scope section for why
this mirrors api_sync_automation's auth)."""
import re

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


class TestStage1Form:
    def test_registering_a_new_automation_redirects_to_stage1(self, app, client):
        with app.app_context():
            _make_user("a@x.com", "A")
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/new").get_data(as_text=True))
        resp = client.post("/automations/new", data={
            "slug": "new-thing", "name": "New Thing", "status": "idea", "csrf_token": token,
        })
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/automations/new-thing/stage1")

    def test_saving_answers_stores_them_keyed_by_phase(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            db.session.add(automation)
            db.session.commit()
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/thing/stage1").get_data(as_text=True))
        resp = client.post("/automations/thing/stage1", data={
            "csrf_token": token,
            "p1_one_liner": "Автоматизує щось корисне",
            "p1_audience": "internal_team",
            "p5_public_attack_surface": "no_internal_only",
            "p4_test_types": ["unit", "integration"],
        })
        assert resp.status_code == 302
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.stage1_answers["1"]["one_liner"] == "Автоматизує щось корисне"
            assert automation.stage1_answers["1"]["audience"] == "internal_team"
            assert automation.stage1_answers["5"]["public_attack_surface"] == "no_internal_only"
            assert automation.stage1_answers["4"]["test_types"] == ["unit", "integration"]
            # Phases with nothing filled in don't show up at all.
            assert "2" not in automation.stage1_answers

    def test_blank_fields_are_not_stored(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            db.session.add(automation)
            db.session.commit()
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/thing/stage1").get_data(as_text=True))
        client.post("/automations/thing/stage1", data={"csrf_token": token, "p1_one_liner": "   "})
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.stage1_answers == {}

    def test_a_non_owner_automator_cannot_fill_someone_elses_form(self, app, client):
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
            other = _make_user("other@x.com", "Other")
            automation = Automation(slug="thing", name="Thing", owner_id=owner.id)
            db.session.add(automation)
            db.session.commit()
        _login(client, "other@x.com")
        resp = client.get("/automations/thing/stage1")
        assert resp.status_code == 403

    def test_admin_can_fill_anyones_form(self, app, client):
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
            _make_user("admin@x.com", "Admin", role=Role.ADMIN)
            automation = Automation(slug="thing", name="Thing", owner_id=owner.id)
            db.session.add(automation)
            db.session.commit()
        _login(client, "admin@x.com")
        resp = client.get("/automations/thing/stage1")
        assert resp.status_code == 200


class TestStage1AnswersApi:
    def test_rejects_missing_key(self, app, client):
        resp = client.get("/api/automations/thing/stage1-answers")
        assert resp.status_code == 401

    def test_rejects_unapproved_account(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A", is_approved=False)
            key = user.api_key
        resp = client.get("/api/automations/thing/stage1-answers", headers={"X-API-Key": key})
        assert resp.status_code == 403

    def test_404_when_no_automation(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            key = user.api_key
        resp = client.get("/api/automations/does-not-exist/stage1-answers", headers={"X-API-Key": key})
        assert resp.status_code == 404

    def test_404_when_no_answers_filled_yet(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            key = user.api_key
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            db.session.add(automation)
            db.session.commit()
        resp = client.get("/api/automations/thing/stage1-answers", headers={"X-API-Key": key})
        assert resp.status_code == 404

    def test_returns_answers_for_the_owner(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            key = user.api_key
            automation = Automation(slug="thing", name="Thing", owner_id=user.id,
                                     stage1_answers={"1": {"one_liner": "X"}})
            db.session.add(automation)
            db.session.commit()
        resp = client.get("/api/automations/thing/stage1-answers", headers={"X-API-Key": key})
        assert resp.status_code == 200
        assert resp.get_json()["answers"] == {"1": {"one_liner": "X"}}

    def test_a_different_owners_key_is_rejected(self, app, client):
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
            other = _make_user("other@x.com", "Other")
            other_key = other.api_key
            automation = Automation(slug="thing", name="Thing", owner_id=owner.id,
                                     stage1_answers={"1": {"one_liner": "X"}})
            db.session.add(automation)
            db.session.commit()
        resp = client.get("/api/automations/thing/stage1-answers", headers={"X-API-Key": other_key})
        assert resp.status_code == 403

    def test_admin_key_can_read_anyones_answers(self, app, client):
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
            admin = _make_user("admin@x.com", "Admin", role=Role.ADMIN)
            admin_key = admin.api_key
            automation = Automation(slug="thing", name="Thing", owner_id=owner.id,
                                     stage1_answers={"1": {"one_liner": "X"}})
            db.session.add(automation)
            db.session.commit()
        resp = client.get("/api/automations/thing/stage1-answers", headers={"X-API-Key": admin_key})
        assert resp.status_code == 200
