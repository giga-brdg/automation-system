"""Covers dashboard/metrics.json sync (src/metrics_contract.py validation,
src/metrics_sync.py snapshots/metrics/measurements/journal/confirmations/
status) and the journal of manual ROI edits.

tests/fixtures/metrics_hrsupplax.json is HRsupplax's real file as
automation-portfolio-sync generated it before the automator answered
anything: no primary metric, no target, no check_every_days, no open
questions. By the contract that file is invalid, and one test checks it is
rejected for exactly those reasons. `_hr_answered()` adds the missing
answers as TEST DATA (which metric is primary, open questions for target and
frequency) - they are not HRsupplax's real answers."""
import copy
import datetime
import json
import re
from pathlib import Path

from src import github_sync, metrics_contract, metrics_sync
from src.extensions import db
from src.models import (
    Automation,
    AutomationEvent,
    AutomationMetric,
    MetricConfirmation,
    MetricMeasurement,
    MetricsSnapshot,
    ROIEntry,
    Role,
    User,
)

FIXTURE = Path(__file__).parent / "fixtures" / "metrics_hrsupplax.json"
TODAY = datetime.date(2026, 10, 26)
SHA1 = "a" * 40
SHA2 = "b" * 40


def _hr_raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _hr_answered():
    data = _hr_raw()
    for m in data["metrics"]:
        m["primary"] = m["id"] == "stuck-on-stage"
    data["open_questions"] = [
        q for m in data["metrics"] for q in (
            {"metric_id": m["id"], "field": "target", "question": "Яка ціль?",
             "ask_whom": "department_lead", "since": "2026-10-26"},
            {"metric_id": m["id"], "field": "check_every_days", "question": "Як часто міряти?",
             "ask_whom": "automator", "since": "2026-10-26"},
        )]
    return data


def _csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _make_user(email, name, role=Role.AUTOMATOR):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=True)
    user.set_password("pw12345")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, email):
    client.post("/login", data={"email": email, "password": "pw12345",
                                "csrf_token": _csrf(client.get("/login").get_data(as_text=True))})


def _make_automation(owner):
    automation = Automation(slug="hrsupplax", name="HRsupplax", owner_id=owner.id,
                            repo_url="https://github.com/giga-brdg/HRsupplax")
    db.session.add(automation)
    db.session.commit()
    return automation


def _stub_github(monkeypatch, metrics_text, sha=SHA1, roi_text=None):
    files = {"dashboard/SUMMARY.md": "## Name\nHRsupplax\n\n## What it does\nБот для HR.\n",
             "dashboard/metrics.json": metrics_text, "dashboard/ROI.md": roi_text}
    monkeypatch.setattr(github_sync, "fetch_raw_file", lambda o, r, path, b: files.get(path))
    monkeypatch.setattr(github_sync, "default_branch", lambda o, r: "main")
    monkeypatch.setattr(github_sync, "fetch_head_sha", lambda o, r, b: sha)


def _resync(client, app, monkeypatch, data, sha=SHA1, roi_text=None):
    text = data if data is None or isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    _stub_github(monkeypatch, text, sha=sha, roi_text=roi_text)
    html = client.get("/automations/hrsupplax").get_data(as_text=True)
    return client.post("/automations/hrsupplax/resync", data={"csrf_token": _csrf(html)},
                       follow_redirects=True)


def _events(event_type=None):
    q = AutomationEvent.query
    if event_type:
        q = q.filter_by(event_type=event_type)
    return q.order_by(AutomationEvent.id).all()


def _setup_owner(app, client, role=Role.ADMIN):
    with app.app_context():
        user = _make_user("admin@x.com", "Оксана", role=role)
        _make_automation(user)
    _login(client, "admin@x.com")


class TestContractValidation:
    def test_raw_hrsupplax_file_fails_for_exactly_the_unanswered_fields(self):
        _, errors, _ = metrics_contract.parse_and_validate(FIXTURE.read_text(encoding="utf-8"), today=TODAY)
        assert any("рівно одна, зараз 0" in e for e in errors)
        missing = sorted(re.search(r"поле (\S+) порожнє", e).group(1) for e in errors if "порожнє" in e)
        assert missing == ["check_every_days"] * 4 + ["target"] * 4
        assert len(errors) == 9

    def test_answered_hrsupplax_file_is_valid(self):
        errors, _ = metrics_contract.validate(_hr_answered(), today=TODAY)
        assert errors == []

    def test_invalid_json_is_one_error_and_no_data(self):
        data, errors, _ = metrics_contract.parse_and_validate("{not json", today=TODAY)
        assert data is None and len(errors) == 1 and "JSON" in errors[0]

    def test_measured_with_text_period_is_rejected(self):
        data = _hr_answered()
        data["metrics"][0]["measurements"][0]["period"] = "вересень"
        errors, _ = metrics_contract.validate(data, today=TODAY)
        assert any("YYYY-MM-DD..YYYY-MM-DD" in e for e in errors)

    def test_roi_md_measured_without_measured_measurement_is_rejected(self):
        data = _hr_answered()
        for m in data["metrics"]:
            m["measurements"] = []
        errors, _ = metrics_contract.validate(data, today=TODAY, roi_md="## Confidence\nMeasured\n")
        assert any("Measured" in e for e in errors)


class TestSync:
    def test_hrsupplax_sync_creates_metrics_measurements_and_measured_status(self, app, client, monkeypatch):
        _setup_owner(app, client)
        resp = _resync(client, app, monkeypatch, _hr_answered())
        assert resp.status_code == 200
        with app.app_context():
            automation = Automation.query.filter_by(slug="hrsupplax").one()
            assert AutomationMetric.query.count() == 4
            assert MetricMeasurement.query.count() == 4
            snapshot = MetricsSnapshot.query.one()
            assert snapshot.is_valid and snapshot.commit_sha == SHA1
            assert snapshot.triggered_by.name == "Оксана"
            overview = metrics_sync.automation_metrics(automation, today=TODAY)
            assert overview["format"] == "metrics"
            assert [m["metric"].metric_key for m in overview["metrics"]][0] == "stuck-on-stage"
            assert {m["status"] for m in overview["metrics"]} == {"measured"}
            assert {m["label"] for m in overview["metrics"]} == {"Виміряно"}
            assert any(q["field"] == "target" for q in overview["open_questions"])
            assert len(_events("metric_added")) == 4
            assert len(_events("measurement_added")) == 4
            assert len(_events("sync_ok")) == 1

    def test_without_metrics_file_nothing_changes(self, app, client, monkeypatch):
        _setup_owner(app, client)
        resp = _resync(client, app, monkeypatch, None)
        assert "Оновлено з GitHub." in resp.get_data(as_text=True)
        with app.app_context():
            assert MetricsSnapshot.query.count() == 0
            assert AutomationMetric.query.count() == 0
            assert AutomationEvent.query.count() == 0
            automation = Automation.query.filter_by(slug="hrsupplax").one()
            assert metrics_sync.automation_metrics(automation, today=TODAY)["format"] == "old"

    def test_invalid_json_applies_nothing_and_is_journaled(self, app, client, monkeypatch):
        _setup_owner(app, client)
        _resync(client, app, monkeypatch, _hr_answered())
        resp = _resync(client, app, monkeypatch, "{broken", sha=SHA2)
        assert "metrics.json не прийнято" in resp.get_data(as_text=True)
        with app.app_context():
            snapshots = MetricsSnapshot.query.order_by(MetricsSnapshot.id).all()
            assert [s.is_valid for s in snapshots] == [True, False]
            assert snapshots[1].raw_json == "{broken"
            assert AutomationMetric.query.filter_by(is_active=True).count() == 4
            failed = _events("sync_failed")
            assert len(failed) == 1 and failed[0].commit_sha == SHA2
            assert "JSON" in failed[0].new_value["errors"][0]

    def test_contract_violation_applies_nothing_not_even_partly(self, app, client, monkeypatch):
        _setup_owner(app, client)
        _resync(client, app, monkeypatch, _hr_answered())
        bad = _hr_answered()
        bad["metrics"][0]["baseline"]["value"] = 999  # would be a change if it got through
        bad["metrics"][1]["measurements"].append(
            {"date": "2026-10-20", "period": "2026-07-22..2026-10-20", "value": 10,
             "source": "тест", "kind": "measured", "note": ""})
        bad["metrics"][2]["direction"] = "sideways"  # the one real violation
        _resync(client, app, monkeypatch, bad, sha=SHA2)
        with app.app_context():
            assert MetricMeasurement.query.count() == 4
            metric = AutomationMetric.query.filter_by(metric_key=bad["metrics"][0]["id"]).one()
            assert metric.baseline["value"] == 15
            assert len(_events("sync_failed")) == 1
            assert _events("baseline_changed") == []

    def test_resync_of_same_sha_does_not_duplicate(self, app, client, monkeypatch):
        _setup_owner(app, client)
        _resync(client, app, monkeypatch, _hr_answered())
        _resync(client, app, monkeypatch, _hr_answered())
        with app.app_context():
            assert MetricMeasurement.query.count() == 4
            assert AutomationMetric.query.count() == 4
            assert len(_events("measurement_added")) == 4
            assert len(_events("metric_added")) == 4

    def test_changed_old_measurement_keeps_stored_value_and_is_journaled(self, app, client, monkeypatch):
        _setup_owner(app, client)
        _resync(client, app, monkeypatch, _hr_answered())
        changed = _hr_answered()
        changed["metrics"][0]["measurements"][0]["value"] = 11
        _resync(client, app, monkeypatch, changed, sha=SHA2)
        _resync(client, app, monkeypatch, changed, sha=SHA2)  # same discrepancy, logged once
        with app.app_context():
            key = changed["metrics"][0]["id"]
            row = MetricMeasurement.query.filter_by(metric_key=key).one()
            assert row.value == 12
            events = _events("measurement_changed_in_repo")
            assert len(events) == 1
            assert events[0].old_value["value"] == 12 and events[0].new_value["value"] == 11

    def test_removed_measurement_is_kept_and_journaled(self, app, client, monkeypatch):
        _setup_owner(app, client)
        _resync(client, app, monkeypatch, _hr_answered())
        removed = _hr_answered()
        removed["metrics"][0]["measurements"] = []
        _resync(client, app, monkeypatch, removed, sha=SHA2)
        with app.app_context():
            assert MetricMeasurement.query.count() == 4
            event = _events("measurement_changed_in_repo")[0]
            assert event.new_value is None

    def test_field_changes_and_removed_metric_are_journaled(self, app, client, monkeypatch):
        _setup_owner(app, client)
        _resync(client, app, monkeypatch, _hr_answered())
        data = _hr_answered()
        stuck = next(m for m in data["metrics"] if m["id"] == "stuck-on-stage")
        stuck["target"] = {"value": 10, "why": "тестове значення"}
        stuck["check_every_days"] = 30
        data["open_questions"] = [q for q in data["open_questions"] if q["metric_id"] != "stuck-on-stage"]
        data["metrics"] = [m for m in data["metrics"] if m["id"] != "no-responses"]
        data["open_questions"] = [q for q in data["open_questions"] if q["metric_id"] != "no-responses"]
        _resync(client, app, monkeypatch, data, sha=SHA2)
        with app.app_context():
            assert _events("target_changed")[0].new_value == {"value": 10, "why": "тестове значення"}
            assert _events("method_changed")[0].new_value["check_every_days"] == 30
            assert _events("metric_removed")[0].metric_key == "no-responses"
            metric = AutomationMetric.query.filter_by(metric_key="no-responses").one()
            assert metric.is_active is False
            assert MetricMeasurement.query.filter_by(metric_key="no-responses").count() == 1


class TestConfirmation:
    def _confirm(self, client, metric_key="stuck-on-stage", **data):
        html = client.get("/automations/hrsupplax").get_data(as_text=True)
        return client.post(f"/automations/hrsupplax/metrics/{metric_key}/confirm",
                           data={"csrf_token": _csrf(html), **data})

    def test_admin_confirms_with_comment(self, app, client, monkeypatch):
        _setup_owner(app, client)
        _resync(client, app, monkeypatch, _hr_answered())
        with app.app_context():
            snapshot_id = MetricsSnapshot.query.one().id
        resp = self._confirm(client, snapshot_id=snapshot_id, verdict="partial", comment="Звірила з PeopleForce.")
        assert resp.status_code == 302
        with app.app_context():
            confirmation = MetricConfirmation.query.one()
            assert confirmation.verdict == "partial" and confirmation.confirmed_by.name == "Оксана"
            automation = Automation.query.filter_by(slug="hrsupplax").one()
            stuck = next(m for m in metrics_sync.automation_metrics(automation, today=TODAY)["metrics"]
                         if m["metric"].metric_key == "stuck-on-stage")
            assert stuck["status"] == "confirmed" and stuck["verdict"] == "partial"
            event = _events("confirmed")[0]
            assert event.commit_sha == SHA1 and event.new_value["comment"] == "Звірила з PeopleForce."

    def test_non_admin_is_forbidden(self, app, client, monkeypatch):
        _setup_owner(app, client, role=Role.AUTOMATOR)
        _resync(client, app, monkeypatch, _hr_answered())
        with app.app_context():
            snapshot_id = MetricsSnapshot.query.one().id
        resp = self._confirm(client, snapshot_id=snapshot_id, verdict="confirmed", comment="ок")
        assert resp.status_code == 403
        with app.app_context():
            assert MetricConfirmation.query.count() == 0
            automation = Automation.query.one()
            user = User.query.one()
            try:
                metrics_sync.confirm_metric(automation, "stuck-on-stage", snapshot_id, "confirmed", "ок", user)
            except PermissionError:
                pass
            else:
                raise AssertionError("an automator must not be able to confirm")

    def test_without_comment_is_refused(self, app, client, monkeypatch):
        _setup_owner(app, client)
        _resync(client, app, monkeypatch, _hr_answered())
        with app.app_context():
            snapshot_id = MetricsSnapshot.query.one().id
        resp = self._confirm(client, snapshot_id=snapshot_id, verdict="confirmed", comment="   ")
        assert resp.status_code == 302
        with app.app_context():
            assert MetricConfirmation.query.count() == 0
            assert _events("confirmed") == []

    def test_csrf_is_required(self, app, client, monkeypatch):
        _setup_owner(app, client)
        _resync(client, app, monkeypatch, _hr_answered())
        client.post("/automations/hrsupplax/metrics/stuck-on-stage/confirm",
                    data={"snapshot_id": 1, "verdict": "confirmed", "comment": "ок"})
        with app.app_context():
            assert MetricConfirmation.query.count() == 0

    def test_new_measurement_resets_confirmation(self, app, client, monkeypatch):
        _setup_owner(app, client)
        _resync(client, app, monkeypatch, _hr_answered())
        with app.app_context():
            snapshot_id = MetricsSnapshot.query.one().id
        self._confirm(client, snapshot_id=snapshot_id, verdict="confirmed", comment="ок")
        data = _hr_answered()
        stuck = next(m for m in data["metrics"] if m["id"] == "stuck-on-stage")
        stuck["measurements"].append({"date": "2026-10-22", "period": "2026-07-22..2026-10-22", "value": 15,
                                      "source": "тест", "kind": "measured", "note": ""})
        _resync(client, app, monkeypatch, data, sha=SHA2)
        with app.app_context():
            confirmation = MetricConfirmation.query.one()
            assert confirmation.invalidated_at is not None and confirmation.invalidated_reason == "новий замір"
            automation = Automation.query.one()
            stuck_status = next(m for m in metrics_sync.automation_metrics(automation, today=TODAY)["metrics"]
                                if m["metric"].metric_key == "stuck-on-stage")
            assert stuck_status["status"] == "needs_review"
            assert stuck_status["label"] == "Потребує перевірки"
            reset = _events("confirmation_reset")
            assert len(reset) == 1 and reset[0].metric_key == "stuck-on-stage"

    def test_unrelated_resync_keeps_confirmation(self, app, client, monkeypatch):
        _setup_owner(app, client)
        _resync(client, app, monkeypatch, _hr_answered())
        with app.app_context():
            snapshot_id = MetricsSnapshot.query.one().id
        self._confirm(client, snapshot_id=snapshot_id, verdict="confirmed", comment="ок")
        _resync(client, app, monkeypatch, _hr_answered(), sha=SHA2)
        with app.app_context():
            assert MetricConfirmation.query.one().invalidated_at is None
            assert _events("confirmation_reset") == []


class TestStatus:
    METRIC = {"baseline": {"value": 28, "period": "2026-05-20..2026-07-21", "source": "s", "kind": "measured"},
              "target": None, "direction": "down", "check_every_days": 30,
              "money": {"rate_usd_per_unit": 8, "realization": 0.5, "realization_why": "тест"}}
    LAUNCH = datetime.date(2026, 7, 22)

    def _m(self, date, value, kind="measured"):
        return {"date": date, "period": "p", "value": value, "kind": kind}

    def test_no_data(self):
        r = metrics_sync.compute_status(dict(self.METRIC, baseline=None), [], self.LAUNCH, TODAY)
        assert r["status"] == "no_data" and r["label"] == "Немає даних"

    def test_awaiting_when_only_baseline(self):
        r = metrics_sync.compute_status(self.METRIC, [], self.LAUNCH, datetime.date(2026, 8, 1))
        assert r["status"] == "awaiting" and r["label"] == "Очікуємо замір"
        assert r["overdue"] is False and r["next_due"] == datetime.date(2026, 8, 21)

    def test_overdue_on_a_fixed_date(self):
        r = metrics_sync.compute_status(self.METRIC, [self._m(datetime.date(2026, 9, 22), 16)],
                                        self.LAUNCH, datetime.date(2026, 10, 22))
        assert r["next_due"] == datetime.date(2026, 10, 22) and r["overdue"] is False
        r = metrics_sync.compute_status(self.METRIC, [self._m(datetime.date(2026, 9, 22), 16)],
                                        self.LAUNCH, datetime.date(2026, 10, 23))
        assert r["overdue"] is True

    def test_measured_result_and_money_respect_direction(self):
        r = metrics_sync.compute_status(self.METRIC, [self._m(datetime.date(2026, 9, 22), 16)],
                                        self.LAUNCH, TODAY)
        assert r["status"] == "measured"
        assert r["improvement"] == 12
        assert r["money_measured"] == 12 * 8 * 0.5
        assert r["money_estimate"] is None
        up = metrics_sync.compute_status(dict(self.METRIC, direction="up"),
                                         [self._m(datetime.date(2026, 9, 22), 16)], self.LAUNCH, TODAY)
        assert up["improvement"] == -12

    def test_estimate_money_is_separate_from_measured(self):
        r = metrics_sync.compute_status(self.METRIC, [self._m(datetime.date(2026, 9, 22), 20, kind="estimate")],
                                        self.LAUNCH, TODAY)
        assert r["status"] == "awaiting"
        assert r["money_measured"] is None and r["money_estimate"] == 8 * 8 * 0.5

    def test_measurement_before_launch_is_not_a_result(self):
        r = metrics_sync.compute_status(self.METRIC, [self._m(datetime.date(2026, 7, 1), 16)], self.LAUNCH, TODAY)
        assert r["status"] == "awaiting" and r["improvement"] is None

    def test_missing_data_is_none_never_zero(self):
        no_baseline = metrics_sync.compute_status(dict(self.METRIC, baseline=None),
                                                  [self._m(datetime.date(2026, 9, 22), 16)], self.LAUNCH, TODAY)
        assert no_baseline["status"] == "measured"
        assert no_baseline["baseline"] is None
        assert no_baseline["improvement"] is None
        assert no_baseline["money_measured"] is None
        no_money = metrics_sync.compute_status(dict(self.METRIC, money=None),
                                               [self._m(datetime.date(2026, 9, 22), 16)], self.LAUNCH, TODAY)
        assert no_money["improvement"] == 12 and no_money["money_measured"] is None
        text_value = metrics_sync.compute_status(self.METRIC, [self._m(datetime.date(2026, 9, 22), "≈16")],
                                                 self.LAUNCH, TODAY)
        assert text_value["latest_measured"] is None and text_value["money_measured"] is None
        no_launch = metrics_sync.compute_status(self.METRIC, [self._m(datetime.date(2026, 9, 22), 16)], None, TODAY)
        assert no_launch["improvement"] is None and no_launch["next_due"] is None


class TestManualRoiJournal:
    def test_measured_hours_edit_is_journaled_with_old_and_new(self, app, client):
        with app.app_context():
            user = _make_user("owner@x.com", "Власник")
            automation = _make_automation(user)
            automation.roi = ROIEntry(measured_hours_per_month=10, confidence="estimated")
            db.session.commit()
        _login(client, "owner@x.com")
        html = client.get("/automations/hrsupplax/edit").get_data(as_text=True)
        resp = client.post("/automations/hrsupplax/edit", data={
            "csrf_token": _csrf(html), "name": "HRsupplax", "status": "live",
            "confidence": "estimated", "measured_hours_per_month": "12.5"})
        assert resp.status_code == 302
        with app.app_context():
            events = _events("roi_field_changed")
            assert len(events) == 1
            event = events[0]
            assert event.field == "measured_hours_per_month"
            assert event.old_value == 10 and event.new_value == 12.5
            assert event.actor.name == "Власник" and event.actor_label == "Власник"

    def test_unchanged_save_writes_nothing(self, app, client):
        with app.app_context():
            user = _make_user("owner@x.com", "Власник")
            automation = _make_automation(user)
            automation.roi = ROIEntry(measured_hours_per_month=10, confidence="estimated")
            db.session.commit()
        _login(client, "owner@x.com")
        html = client.get("/automations/hrsupplax/edit").get_data(as_text=True)
        client.post("/automations/hrsupplax/edit", data={
            "csrf_token": _csrf(html), "name": "HRsupplax", "status": "live",
            "confidence": "estimated", "measured_hours_per_month": "10"})
        with app.app_context():
            assert _events("roi_field_changed") == []

    def test_stage1_time_answers_are_journaled(self, app, client):
        with app.app_context():
            user = _make_user("owner@x.com", "Власник")
            _make_automation(user)
        _login(client, "owner@x.com")
        html = client.get("/automations/hrsupplax/stage1").get_data(as_text=True)
        client.post("/automations/hrsupplax/stage1", data={
            "csrf_token": _csrf(html), "p1_baseline_cycle_minutes": "45", "p1_baseline_frequency_per_month": "12"})
        with app.app_context():
            fields = {e.field: (e.old_value, e.new_value) for e in _events("roi_field_changed")}
            assert fields.get("baseline_cycle_minutes") == (None, 45.0)
            assert fields.get("baseline_frequency_per_month") == (None, 12.0)


def test_fixture_is_unchanged_copy():
    """The fixture must stay HRsupplax's file as generated - test answers are
    added in code, never written back into it."""
    data = _hr_raw()
    assert copy.deepcopy(data)["open_questions"] == []
    assert all(m["primary"] is False for m in data["metrics"])
