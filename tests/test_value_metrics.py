"""Covers /value on top of dashboard/metrics.json data (src/value_dashboard.py's
build_context/build_row and the template): tiles by the primary metric's
status, money split by Oksana's verdict, old-format fallback, rejected
files, status filter, and the confirmation form.

HRsupplax data comes from tests/fixtures/metrics_hrsupplax.json; the
"answered" variant adds TEST answers in code (see test_metrics_sync.py's
module docstring) - not HRsupplax's real answers."""
import datetime
import json
import re

from test_metrics_sync import FIXTURE, _hr_answered

from src import metrics_sync
from src.extensions import db
from src.models import Automation, MetricConfirmation, MetricsSnapshot, ROIEntry, Role, Status, User
from src.value_dashboard import build_context

TODAY = datetime.date(2026, 10, 26)
LAUNCH = "2026-07-22"


def _user(email, name, role=Role.ADMIN):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=True)
    user.set_password("pw12345")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, email):
    html = client.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    client.post("/login", data={"email": email, "password": "pw12345", "csrf_token": token})


def _automation(owner, slug, status=Status.LIVE, **kw):
    a = Automation(slug=slug, name=slug, owner_id=owner.id, status=status, **kw)
    db.session.add(a)
    db.session.commit()
    return a


def _sync(automation, data, actor=None):
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    warnings = metrics_sync.apply_metrics_file(automation, text, commit_sha="c" * 40, actor=actor, today=TODAY)
    db.session.commit()
    return warnings


def _time_doc(after=4, money=True, measured=True, extra_measurements=()):
    """A one-metric time file: 10 год/міс before, `after` год/міс measured
    after launch, money 8 $/год × 0.5. Synthetic test data."""
    measurements = []
    if after is not None:
        measurements.append({"date": "2026-09-22", "period": "2026-07-22..2026-09-21", "value": after,
                             "source": "тест", "kind": "measured" if measured else "estimate", "note": ""})
    measurements.extend(extra_measurements)
    return {
        "version": 1, "launched_at": LAUNCH,
        "metrics": [{
            "id": "hours", "name": "Години ручної роботи", "type": "time", "unit": "год/міс",
            "direction": "down", "primary": True,
            "baseline": {"value": 10, "period": "2026-06-01..2026-06-30", "source": "тест", "kind": "measured"},
            "target": {"value": 2, "why": "тест"}, "method": "тест", "check_every_days": 60,
            "measurements": measurements,
            "money": {"rate_usd_per_unit": 8, "realization": 0.5, "realization_why": "тест"} if money else None,
        }],
        "open_questions": [],
    }


def _confirm(automation, admin, verdict, metric_key="hours"):
    snapshot = metrics_sync.latest_valid_snapshot(automation.id)
    metrics_sync.confirm_metric(automation, metric_key, snapshot.id, verdict, "тестовий коментар", admin)
    db.session.commit()


class TestBuildContextOnMetrics:
    def test_hrsupplax_answered_is_measured(self, app):
        with app.app_context():
            admin = _user("o@x.com", "Оксана")
            hr = _automation(admin, "hrsupplax")
            _sync(hr, _hr_answered())
            ctx = build_context([hr], today=TODAY)
            row = ctx["rows"][0]
            assert row["format"] == "metrics" and row["rejected"] is None
            assert row["primary"]["metric"].metric_key == "stuck-on-stage"
            assert row["primary"]["status"] == "measured"
            assert row["primary"]["trend"] == "better"  # 28 -> 16, direction down
            assert len(row["metrics"]) == 4 and len(row["open_questions"]) == 8
            assert ctx["tiles"] == {"confirmed": 0, "measured": 1, "awaiting": 0, "no_data": 0}
            assert ctx["confidence_pct"] == 100
            assert ctx["saved_total"] is None  # no time metric in HRsupplax
            assert ctx["has_money"] is False and ctx["money_total"] is None
            counts = {m["label"]: m["count"] for m in ctx["metric_counts"]}
            assert counts["Якість"] == 1 and counts["Час ручної праці"] == 0

    def test_hrsupplax_as_is_is_rejected_and_falls_back(self, app):
        with app.app_context():
            admin = _user("o@x.com", "Оксана")
            hr = _automation(admin, "hrsupplax")
            warnings = _sync(hr, FIXTURE.read_text(encoding="utf-8"))
            assert "не прийнято" in warnings[0]
            row = build_context([hr], today=TODAY)["rows"][0]
            assert row["format"] == "old"
            assert len(row["rejected"]["errors"]) == 9

    def test_rejected_after_a_valid_snapshot_keeps_the_valid_data(self, app):
        with app.app_context():
            admin = _user("o@x.com", "Оксана")
            hr = _automation(admin, "hrsupplax")
            _sync(hr, _hr_answered())
            _sync(hr, FIXTURE.read_text(encoding="utf-8"))
            row = build_context([hr], today=TODAY)["rows"][0]
            assert row["format"] == "metrics" and row["primary"]["status"] == "measured"
            assert len(row["rejected"]["errors"]) == 9

    def test_without_metrics_file_is_old_format(self, app):
        with app.app_context():
            owner = _user("o@x.com", "O", role=Role.AUTOMATOR)
            old = _automation(owner, "old", roi=ROIEntry(measured_hours_per_month=6))
            ctx = build_context([old], today=TODAY)
            assert ctx["rows"][0]["format"] == "old"
            assert ctx["old_format_n"] == 1
            assert ctx["tiles"]["measured"] == 1
            assert ctx["saved_total"] == 6 and ctx["saved_measured"] == 6

    def test_metric_without_data_is_none_not_zero(self, app):
        with app.app_context():
            owner = _user("o@x.com", "O")
            a = _automation(owner, "empty")
            doc = _time_doc(after=None)
            doc["metrics"][0]["baseline"] = None
            doc["open_questions"] = [{"metric_id": "hours", "field": "baseline", "question": "Скільки було?",
                                      "ask_whom": "automator", "since": "2026-10-01"}]
            _sync(a, doc)
            ctx = build_context([a], today=TODAY)
            p = ctx["rows"][0]["primary"]
            assert p["status"] == "no_data"
            assert p["baseline"] is None and p["latest_measured"] is None and p["improvement"] is None
            assert p["money_measured"] is None and p["money_estimate"] is None
            assert ctx["tiles"]["no_data"] == 1
            assert ctx["saved_total"] is None and ctx["money_total"] is None

    def test_time_metric_feeds_saved_hours(self, app):
        with app.app_context():
            owner = _user("o@x.com", "O")
            a = _automation(owner, "time")
            _sync(a, _time_doc(after=4))
            old = _automation(owner, "old", roi=ROIEntry(baseline_cycle_minutes=60, baseline_frequency_per_month=3,
                                                         target_cycle_minutes=0))
            ctx = build_context([a, old], today=TODAY)
            assert ctx["saved_measured"] == 6  # 10 - 4 from metrics.json
            assert ctx["saved_estimated"] == 3  # old-format Stage 1 estimate
            assert ctx["saved_total"] == 9

    def test_money_split_by_every_verdict(self, app):
        with app.app_context():
            admin = _user("o@x.com", "Оксана")
            autos = {}
            for slug in ("confirmed", "partial", "rejected", "unconfirmed", "estimate", "stale", "nomoney"):
                autos[slug] = _automation(admin, slug)
            for slug in ("confirmed", "partial", "rejected", "unconfirmed", "stale"):
                _sync(autos[slug], _time_doc(after=4))  # 6 год × 8 × 0.5 = $24
            _sync(autos["estimate"], _time_doc(after=5, measured=False))  # 5 × 8 × 0.5 = $20
            _sync(autos["nomoney"], _time_doc(after=4, money=False))
            _confirm(autos["confirmed"], admin, "confirmed")
            _confirm(autos["partial"], admin, "partial")
            _confirm(autos["rejected"], admin, "rejected")
            _confirm(autos["stale"], admin, "confirmed")
            _sync(autos["stale"], _time_doc(after=4, extra_measurements=[
                {"date": "2026-10-20", "period": "2026-09-22..2026-10-19", "value": 4,
                 "source": "тест", "kind": "measured", "note": ""}]))
            ctx = build_context(list(autos.values()), today=TODAY)
            assert ctx["money"]["confirmed"] == 24
            assert ctx["money"]["measured"] == 24 * 3  # partial + unconfirmed + stale
            assert ctx["money"]["estimate"] == 20
            assert ctx["money"]["rejected"] == 24
            assert ctx["money_total"] == 24 + 72 + 20  # rejected stays out
            # "частково" counts as confirmed on the tile (money keeps it in
            # "виміряно, не підтверджено"); "не підтверджено" stays measured.
            assert ctx["tiles"]["confirmed"] == 2 and ctx["partial_n"] == 1
            assert ctx["tiles"]["measured"] == 4  # rejected, unconfirmed, stale, nomoney
            assert ctx["rejected_verdict_n"] == 1
            assert ctx["tiles"]["awaiting"] == 1  # estimate only

    def test_new_measurement_resets_confirmation_on_the_page(self, app):
        with app.app_context():
            admin = _user("o@x.com", "Оксана")
            a = _automation(admin, "a")
            _sync(a, _time_doc(after=4))
            _confirm(a, admin, "confirmed")
            assert build_context([a], today=TODAY)["tiles"]["confirmed"] == 1
            _sync(a, _time_doc(after=4, extra_measurements=[
                {"date": "2026-10-20", "period": "2026-09-22..2026-10-19", "value": 3,
                 "source": "тест", "kind": "measured", "note": ""}]))
            ctx = build_context([a], today=TODAY)
            p = ctx["rows"][0]["primary"]
            assert p["status"] == "needs_review" and p["label"] == "Потребує перевірки"
            assert p["confirmation"]["reset_reason"] == "новий замір"
            assert ctx["tiles"]["confirmed"] == 0 and ctx["tiles"]["measured"] == 1
            assert ctx["money"]["confirmed"] == 0 and ctx["money"]["measured"] == 28  # 7 × 8 × 0.5
            events = [e["text"] for e in ctx["rows"][0]["events"]]
            assert "Підтвердження скинуто (Години ручної роботи): новий замір" in events

    def test_overdue_on_fixed_date(self, app):
        with app.app_context():
            owner = _user("o@x.com", "O")
            a = _automation(owner, "a")
            _sync(a, _time_doc(after=4))  # last measurement 22.09, every 60 days -> due 21.11
            on_time = build_context([a], today=datetime.date(2026, 11, 21))["rows"][0]["primary"]
            late = build_context([a], today=datetime.date(2026, 11, 22))["rows"][0]["primary"]
            assert on_time["overdue"] is False and late["overdue"] is True
            assert late["next_due"] == datetime.date(2026, 11, 21)


class TestValueRoute:
    def test_ideas_and_archive_excluded_by_default(self, app, client):
        with app.app_context():
            owner = _user("o@x.com", "Оксана")
            _automation(owner, "live-one", roi=ROIEntry(measured_hours_per_month=5))
            _automation(owner, "idea-one", status=Status.IDEA, roi=ROIEntry(measured_hours_per_month=50))
            _automation(owner, "archived-one", status=Status.ARCHIVED, roi=ROIEntry(measured_hours_per_month=500))
        _login(client, "o@x.com")
        html = client.get("/value").get_data(as_text=True)
        assert "live-one" in html and "idea-one" not in html and "archived-one" not in html
        assert "5.0 <span" in html  # big card counts the live one only
        all_html = client.get("/value?status=all").get_data(as_text=True)
        assert "idea-one" in all_html and "555.0 <span" in all_html

    def test_no_money_shows_dash_not_zero(self, app, client):
        with app.app_context():
            owner = _user("o@x.com", "Оксана")
            _automation(owner, "a")
        _login(client, "o@x.com")
        html = client.get("/value").get_data(as_text=True)
        assert "грошей ще не рахували" in html and "$0.00" not in html

    def test_rejected_badge_and_errors_render(self, app, client):
        with app.app_context():
            owner = _user("o@x.com", "Оксана")
            hr = _automation(owner, "hrsupplax")
            _sync(hr, FIXTURE.read_text(encoding="utf-8"))
        _login(client, "o@x.com")
        html = client.get("/value").get_data(as_text=True)
        assert "файл метрик відхилено (9 помилок)" in html
        assert "Головна метрика (primary: true) має бути рівно одна" in html

    def test_rejected_only_row_shows_one_badge_and_a_hint(self, app, client):
        with app.app_context():
            owner = _user("o@x.com", "Оксана")
            hr = _automation(owner, "hrsupplax")
            _sync(hr, FIXTURE.read_text(encoding="utf-8"))
        _login(client, "o@x.com")
        html = client.get("/value").get_data(as_text=True)
        row = html[html.index(">hrsupplax</a>"):html.index("</details>")]
        assert "Невідомо" not in row and "старий формат" not in row and "Немає ROI" not in row
        assert "Не вибрано головну метрику." in row
        assert "не заповнено «Ціль», і не записано, кого про це спитати." in row
        assert "запустити automation-portfolio-sync у репо, він спитає відсутнє" in row

    def test_confirm_form_only_for_admin(self, app, client):
        with app.app_context():
            admin = _user("admin@x.com", "Оксана")
            automator = _user("auto@x.com", "Автоматизатор", role=Role.AUTOMATOR)
            hr = _automation(automator, "hrsupplax")
            _sync(hr, _hr_answered())
        _login(client, "auto@x.com")
        html = client.get("/value").get_data(as_text=True)
        assert "Деталі: 4 метрик" in html and ">Підтвердити</button>" not in html
        client.get("/logout")
        _login(client, "admin@x.com")
        html = client.get("/value").get_data(as_text=True)
        # One form for the primary metric above the list, a small one per
        # other metric inside "Інші метрики".
        assert html.count(">Підтвердити</button>") == 4
        assert html.count("Вердикт щодо «") == 1
        assert "Вердикт щодо «Заявки, що застрягли на етапі довше ліміту»" in html
        assert "Інші метрики (3)" in html
        assert "не задано, питання до: керівник відділу" in html
        assert "20.05–21.07.2026" in html and "2026-05-20..2026-07-21" not in html

    def test_confirm_posts_back_to_value_with_filters(self, app, client):
        with app.app_context():
            admin = _user("admin@x.com", "Оксана")
            hr = _automation(admin, "hrsupplax")
            _sync(hr, _hr_answered())
            snapshot_id = MetricsSnapshot.query.one().id
        _login(client, "admin@x.com")
        html = client.get(f"/value?owner=1&status=live").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        next_url = re.search(r'name="next" value="([^"]+)"', html).group(1).replace("&amp;", "&")
        assert next_url == "/value?owner=1&status=live"

        resp = client.post("/automations/hrsupplax/metrics/stuck-on-stage/confirm", data={
            "snapshot_id": snapshot_id, "verdict": "partial", "comment": "ок", "next": next_url})
        assert resp.status_code == 302  # CSRF error handler redirects back
        resp = client.post("/automations/hrsupplax/metrics/stuck-on-stage/confirm", data={
            "csrf_token": token, "snapshot_id": snapshot_id, "verdict": "partial", "comment": "  ", "next": next_url})
        assert resp.headers["Location"].endswith(next_url)
        with app.app_context():
            assert MetricConfirmation.query.count() == 0

        resp = client.post("/automations/hrsupplax/metrics/stuck-on-stage/confirm", data={
            "csrf_token": token, "snapshot_id": snapshot_id, "verdict": "partial",
            "comment": "Звірила з PeopleForce.", "next": next_url})
        assert resp.headers["Location"].endswith(next_url)
        with app.app_context():
            assert MetricConfirmation.query.one().verdict == "partial"
        html = client.get(next_url).get_data(as_text=True)
        assert "Оксана: частково" in html

    def test_next_outside_value_is_ignored(self, app, client):
        with app.app_context():
            admin = _user("admin@x.com", "Оксана")
            hr = _automation(admin, "hrsupplax")
            _sync(hr, _hr_answered())
            snapshot_id = MetricsSnapshot.query.one().id
        _login(client, "admin@x.com")
        html = client.get("/value").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        resp = client.post("/automations/hrsupplax/metrics/stuck-on-stage/confirm", data={
            "csrf_token": token, "snapshot_id": snapshot_id, "verdict": "confirmed",
            "comment": "ок", "next": "https://evil.example/value"})
        assert resp.headers["Location"].endswith("/automations/hrsupplax")


class TestNetBalance:
    def test_metrics_time_sum_minus_maintenance(self, app):
        with app.app_context():
            owner = _user("o@x.com", "O")
            a = _automation(owner, "time", roi=ROIEntry(maintenance_hours_per_month=1))
            _sync(a, _time_doc(after=4))  # 10 -> 4 год/міс measured
            est = _automation(owner, "est", roi=ROIEntry(maintenance_hours_per_month=2))
            _sync(est, _time_doc(after=5, measured=False))  # estimate only: 5 год/міс
            old = _automation(owner, "old", roi=ROIEntry(measured_hours_per_month=3, maintenance_hours_per_month=1))
            no_maint = _automation(owner, "no-maint")
            _sync(no_maint, _time_doc(after=4))
            ctx = build_context([a, est, old, no_maint], today=TODAY)
            net = {r["automation"].slug: (r["net"], r["measured"]) for r in ctx["net_rows"]}
            assert net == {"time": (5.0, True), "est": (3.0, False), "old": (2.0, True)}
            assert ctx["saved_total"] == 6 + 5 + 3 + 6

    def test_non_hour_time_unit_stays_out(self, app):
        with app.app_context():
            owner = _user("o@x.com", "O")
            a = _automation(owner, "minutes", roi=ROIEntry(maintenance_hours_per_month=1))
            doc = _time_doc(after=4)
            doc["metrics"][0]["unit"] = "хв/тиждень"
            _sync(a, doc)
            ctx = build_context([a], today=TODAY)
            assert ctx["net_rows"] == [] and ctx["saved_total"] is None


class TestMissingFieldText:
    def test_asked_maps_field_to_who(self, app):
        with app.app_context():
            owner = _user("o@x.com", "O")
            hr = _automation(owner, "hrsupplax")
            _sync(hr, _hr_answered())
            p = build_context([hr], today=TODAY)["rows"][0]["primary"]
            assert p["asked"] == {"target": "керівник відділу", "check_every_days": "автоматизатор"}
            q = build_context([hr], today=TODAY)["rows"][0]["open_questions"][0]
            assert q["field_label"] == "Ціль" and q["ask_whom_label"] == "керівник відділу"
            assert q["since_text"] == "26.10.2026"
