"""Covers ROIEntry's structured time-metric columns (models.py) - the computed
baseline/target/saved-hours properties, Stage 1's Phase 1 numbers landing in them
(automation_stage1's apply_stage1_time_metrics, src/app.py), and the manual edit
form's measured_hours_per_month field (apply_manual_form)."""
import re

from src.extensions import db
from src.models import Automation, ROIEntry, Role, User


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


class TestROIEntryComputedProperties:
    def test_hours_per_month_needs_both_inputs(self, app):
        with app.app_context():
            roi = ROIEntry()
            assert roi.baseline_hours_per_month is None
            assert roi.target_hours_per_month is None
            assert roi.estimated_hours_saved_per_month is None

            roi.baseline_cycle_minutes = 45
            assert roi.baseline_hours_per_month is None  # frequency still missing

    def test_baseline_and_target_hours_computed_from_cycle_times_frequency(self, app):
        with app.app_context():
            roi = ROIEntry(
                baseline_cycle_minutes=45, baseline_frequency_per_month=12,
                target_cycle_minutes=5, target_frequency_per_month=12,
            )
            assert roi.baseline_hours_per_month == 9  # 45 * 12 / 60
            assert roi.target_hours_per_month == 1  # 5 * 12 / 60
            assert roi.estimated_hours_saved_per_month == 8

    def test_target_frequency_falls_back_to_baseline_when_blank(self, app):
        with app.app_context():
            roi = ROIEntry(
                baseline_cycle_minutes=60, baseline_frequency_per_month=10,
                target_cycle_minutes=6, target_frequency_per_month=None,
            )
            assert roi.target_hours_per_month == 1  # 6 * 10 (baseline freq) / 60


class TestStage1SavesTimeMetricsIntoROI:
    def test_phase1_numbers_land_in_roi_columns(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            db.session.add(automation)
            db.session.commit()
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/thing/stage1").get_data(as_text=True))
        client.post("/automations/thing/stage1", data={
            "csrf_token": token,
            "p1_baseline_cycle_minutes": "45",
            "p1_baseline_frequency_per_month": "12",
            "p1_target_cycle_minutes": "5",
            "p1_target_frequency_per_month": "12",
        })
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert float(automation.roi.baseline_cycle_minutes) == 45
            assert float(automation.roi.baseline_frequency_per_month) == 12
            assert float(automation.roi.target_cycle_minutes) == 5
            assert automation.roi.target_hours_per_month == 1

    def test_missing_time_fields_do_not_create_an_empty_roi_entry(self, app, client):
        """Phase 1's other free-text fields (one_liner etc.) shouldn't spin up a
        blank ROIEntry with nothing useful in it - only real numbers do."""
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            db.session.add(automation)
            db.session.commit()
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/thing/stage1").get_data(as_text=True))
        client.post("/automations/thing/stage1", data={"csrf_token": token, "p1_one_liner": "Робить щось"})
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.roi is None

    def test_non_numeric_time_field_is_skipped_not_a_500(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            db.session.add(automation)
            db.session.commit()
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/automations/thing/stage1").get_data(as_text=True))
        resp = client.post("/automations/thing/stage1", data={
            "csrf_token": token,
            "p1_baseline_cycle_minutes": "не знаю",
            "p1_baseline_frequency_per_month": "12",
        })
        assert resp.status_code == 302
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.roi.baseline_cycle_minutes is None
            assert float(automation.roi.baseline_frequency_per_month) == 12


class TestManualMeasuredHoursField:
    def test_measured_hours_per_month_saved_from_edit_form(self, app, client):
        with app.app_context():
            user = _make_user("owner@x.com", "Owner")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            db.session.add(automation)
            db.session.commit()
        _login(client, "owner@x.com")
        token = _csrf_token(client.get("/automations/thing/edit").get_data(as_text=True))
        resp = client.post("/automations/thing/edit", data={
            "csrf_token": token, "name": "Thing", "status": "live",
            "measured_hours_per_month": "7.5",
        })
        assert resp.status_code == 302
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert float(automation.roi.measured_hours_per_month) == 7.5

    def test_blank_measured_hours_stays_none(self, app, client):
        with app.app_context():
            user = _make_user("owner@x.com", "Owner")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            db.session.add(automation)
            db.session.commit()
        _login(client, "owner@x.com")
        token = _csrf_token(client.get("/automations/thing/edit").get_data(as_text=True))
        client.post("/automations/thing/edit", data={
            "csrf_token": token, "name": "Thing", "status": "live",
        })
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.roi.measured_hours_per_month is None
