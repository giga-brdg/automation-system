"""Value items (metrics contract v1.1): sync into AutomationValueItem, the
journal, admin verdicts and their reset, the confirm route, /value's column,
items table and "Гроші" buckets, baseline.not_applicable_why, and v1
compatibility. All data here is TEST DATA."""
import datetime
import json
import re

import pytest

from src import metrics_sync, value_dashboard
from src.extensions import db
from src.models import (
    Automation,
    AutomationEvent,
    AutomationMetric,
    AutomationValueItem,
    MetricsSnapshot,
    Role,
    User,
    ValueItemConfirmation,
)

TODAY = datetime.date(2026, 10, 27)


def _metric(**overrides):
    m = {"id": "stuck-on-stage", "name": "ТЕСТ Заявки, що застрягли", "type": "quality", "unit": "%",
         "direction": "down", "primary": True,
         "baseline": {"value": 28, "period": "2026-04-01..2026-07-21", "source": "ТЕСТ звіт",
                      "kind": "measured"},
         "target": {"value": 15, "why": "ТЕСТ ціль"}, "method": "ТЕСТ метод", "check_every_days": 120,
         "measurements": [], "money": None}
    m.update(overrides)
    return m


def _item(**overrides):
    x = {"id": "recruiter-hours", "category": "time", "title": "ТЕСТ Рекрутер не перевіряє воронку",
         "usd_per_month": 36, "kind": "estimate", "basis": "assumption", "formula": "9 год/міс × $8 × 0,5",
         "why": "ТЕСТ 0,5: час іде на дзвінки", "source": "ТЕСТ оцінка автоматизатора",
         "as_of": "2026-10-20", "metric_ids": None}
    x.update(overrides)
    return x


RUN_COST = {"id": "run-cost", "category": "run_cost", "title": "ТЕСТ Хостинг і підтримка",
            "usd_per_month": -25, "kind": "estimate", "basis": "data",
            "formula": "Railway $10 + 1 год/міс × $15", "why": "ТЕСТ рахунок", "source": "ТЕСТ рахунок Railway",
            "as_of": "2026-10-20", "metric_ids": None}


def _doc(*items, metrics=None, questions=None):
    d = {"version": 1, "launched_at": "2026-07-22", "metrics": metrics or [_metric()],
         "open_questions": questions or []}
    if items != (None,):
        d["value_items"] = list(items)
    return d


def _csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _make_user(email, name, role):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=True)
    user.set_password("pw12345")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, email):
    client.post("/login", data={"email": email, "password": "pw12345",
                                "csrf_token": _csrf(client.get("/login").get_data(as_text=True))})


@pytest.fixture
def automation(app):
    owner = _make_user("admin@x.com", "ТЕСТ Адмін", Role.ADMIN)
    a = Automation(slug="test-hr", name="ТЕСТ HR", owner_id=owner.id)
    db.session.add(a)
    db.session.commit()
    return a


def _sync(automation, data, sha="a" * 40, roi_md=None):
    warnings = metrics_sync.apply_metrics_file(
        automation, json.dumps(data, ensure_ascii=False), commit_sha=sha, roi_md=roi_md, today=TODAY)
    db.session.commit()
    return warnings


def _events(event_type):
    return AutomationEvent.query.filter_by(event_type=event_type).order_by(AutomationEvent.id).all()


def _items():
    return {r.item_key: r for r in AutomationValueItem.query}


def _confirm(automation, admin, key="recruiter-hours", verdict="confirmed"):
    snapshot = metrics_sync.latest_valid_snapshot(automation.id)
    c = metrics_sync.confirm_value_item(automation, key, snapshot.id, verdict, "ТЕСТ перевірено", admin)
    db.session.commit()
    return c


# --- Sync and journal ------------------------------------------------------------

class TestSync:
    def test_items_are_stored_and_journaled(self, app, automation):
        assert _sync(automation, _doc(_item(), RUN_COST)) == []
        items = _items()
        assert set(items) == {"recruiter-hours", "run-cost"}
        assert items["run-cost"].usd_per_month == -25.0
        assert items["recruiter-hours"].formula == "9 год/міс × $8 × 0,5"
        added = _events("value_item_added")
        assert [e.metric_key for e in added] == ["recruiter-hours", "run-cost"]

    def test_same_file_again_logs_nothing_new(self, app, automation):
        _sync(automation, _doc(_item(), RUN_COST))
        _sync(automation, _doc(_item(), RUN_COST), sha="b" * 40)
        assert len(_events("value_item_added")) == 2
        assert _events("value_item_changed") == []

    def test_change_logs_old_and_new(self, app, automation):
        _sync(automation, _doc(_item()))
        _sync(automation, _doc(_item(usd_per_month=40, formula="10 год/міс × $8 × 0,5")), sha="b" * 40)
        [event] = _events("value_item_changed")
        assert event.old_value["usd_per_month"] == 36.0 and event.new_value["usd_per_month"] == 40.0
        assert event.old_value["formula"] == "9 год/міс × $8 × 0,5"
        assert event.new_value["formula"] == "10 год/міс × $8 × 0,5"
        text = value_dashboard.describe_event(event, {}, _items())["text"]
        assert "$36 → $40/міс" in text and "формула" in text

    def test_removed_item_is_kept_inactive_and_journaled(self, app, automation):
        _sync(automation, _doc(_item(), RUN_COST))
        _sync(automation, _doc(_item()), sha="b" * 40)
        assert _items()["run-cost"].is_active is False
        [event] = _events("value_item_removed")
        assert event.metric_key == "run-cost"
        assert "прибрано" in value_dashboard.describe_event(event, {}, _items())["text"]

    def test_v1_file_without_value_items_behaves_as_before(self, app, automation):
        _sync(automation, _doc(None))
        assert AutomationValueItem.query.count() == 0
        assert [e.event_type for e in AutomationEvent.query] == ["metric_added", "sync_ok"]

    def test_v1_file_after_v11_removes_items(self, app, automation):
        _sync(automation, _doc(_item()))
        _sync(automation, _doc(None), sha="b" * 40)
        assert _items()["recruiter-hours"].is_active is False

    def test_invalid_file_changes_no_items(self, app, automation):
        _sync(automation, _doc(_item()))
        warnings = _sync(automation, _doc(_item(usd_per_month=-5)), sha="b" * 40)
        assert "не прийнято" in warnings[0]
        assert _items()["recruiter-hours"].usd_per_month == 36.0
        assert MetricsSnapshot.query.count() == 2

    def test_value_items_next_to_money_is_rejected(self, app, automation):
        money = _metric(money={"rate_usd_per_unit": 8, "realization": 0.5, "realization_why": "ТЕСТ"})
        warnings = _sync(automation, _doc(_item(), metrics=[money]))
        assert "не прийнято" in warnings[0] and "money" in warnings[0]
        assert AutomationValueItem.query.count() == 0

    def test_snapshot_keeps_raw_file(self, app, automation):
        data = _doc(_item())
        _sync(automation, data)
        snapshot = MetricsSnapshot.query.one()
        assert snapshot.data == data and snapshot.is_valid


# --- Verdicts --------------------------------------------------------------------

class TestVerdicts:
    def test_confirm_records_verdict_and_event(self, app, automation):
        _sync(automation, _doc(_item()))
        admin = User.query.first()
        c = _confirm(automation, admin)
        assert c.confirmed_state == {"usd_per_month": 36.0, "formula": "9 год/міс × $8 × 0,5", "kind": "estimate"}
        [event] = _events("value_item_confirmed")
        assert event.new_value["verdict"] == "confirmed"
        entry = metrics_sync.automation_value_items(automation)[0]
        assert entry["status"] == "confirmed" and entry["bucket"] == "confirmed"

    @pytest.mark.parametrize("change,reason", [
        ({"usd_per_month": 40}, "змінено $/міс"),
        ({"formula": "інша"}, "змінено формулу"),
        ({"kind": "measured", "basis": "data", "source": "ТЕСТ журнал 2026-09-01..2026-09-30"},
         "змінено статус (замір/оцінка)"),
    ])
    def test_verdict_resets_when_numbers_change(self, app, automation, change, reason):
        _sync(automation, _doc(_item()))
        _confirm(automation, User.query.first())
        _sync(automation, _doc(_item(**change)), sha="b" * 40)
        c = ValueItemConfirmation.query.one()
        assert c.invalidated_at is not None and c.invalidated_reason == reason
        [event] = _events("value_confirmation_reset")
        assert event.new_value["reason"] == reason
        assert metrics_sync.automation_value_items(automation)[0]["status"] != "confirmed"

    def test_verdict_holds_when_only_why_changes(self, app, automation):
        _sync(automation, _doc(_item()))
        _confirm(automation, User.query.first())
        _sync(automation, _doc(_item(why="ТЕСТ інше пояснення")), sha="b" * 40)
        assert ValueItemConfirmation.query.one().invalidated_at is None
        assert _events("value_confirmation_reset") == []

    def test_verdict_on_numbers_admin_did_not_see_is_refused(self, app, automation):
        _sync(automation, _doc(_item()))
        old_snapshot = metrics_sync.latest_valid_snapshot(automation.id)
        _sync(automation, _doc(_item(usd_per_month=40)), sha="b" * 40)
        with pytest.raises(metrics_sync.ConfirmationError, match="змінилась"):
            metrics_sync.confirm_value_item(automation, "recruiter-hours", old_snapshot.id, "confirmed",
                                            "ТЕСТ", User.query.first())

    def test_non_admin_and_empty_comment_are_refused(self, app, automation):
        _sync(automation, _doc(_item()))
        snapshot = metrics_sync.latest_valid_snapshot(automation.id)
        automator = _make_user("auto@x.com", "ТЕСТ Автоматизатор", Role.AUTOMATOR)
        with pytest.raises(PermissionError):
            metrics_sync.confirm_value_item(automation, "recruiter-hours", snapshot.id, "confirmed", "x", automator)
        with pytest.raises(metrics_sync.ConfirmationError, match="Коментар"):
            metrics_sync.confirm_value_item(automation, "recruiter-hours", snapshot.id, "confirmed", " ",
                                            User.query.first())


# --- Route -----------------------------------------------------------------------

class TestConfirmRoute:
    def _post(self, client, data):
        html = client.get("/automations/new").get_data(as_text=True)
        return client.post("/automations/test-hr/value/recruiter-hours/confirm",
                           data=dict({"csrf_token": _csrf(html)}, **data))

    def test_admin_confirms_and_lands_on_value(self, app, client, automation):
        _sync(automation, _doc(_item()))
        _login(client, "admin@x.com")
        snapshot_id = metrics_sync.latest_valid_snapshot(automation.id).id
        resp = self._post(client, {"snapshot_id": snapshot_id, "verdict": "confirmed",
                                   "comment": "ТЕСТ ок", "next": "/value?status=all"})
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/value?status=all")
        assert ValueItemConfirmation.query.count() == 1

    def test_foreign_next_falls_back_to_value(self, app, client, automation):
        _sync(automation, _doc(_item()))
        _login(client, "admin@x.com")
        snapshot_id = metrics_sync.latest_valid_snapshot(automation.id).id
        resp = self._post(client, {"snapshot_id": snapshot_id, "verdict": "confirmed",
                                   "comment": "ТЕСТ", "next": "https://evil.example/"})
        assert resp.headers["Location"].endswith("/value")

    def test_non_admin_gets_403(self, app, client, automation):
        _sync(automation, _doc(_item()))
        _make_user("auto@x.com", "ТЕСТ Автоматизатор", Role.AUTOMATOR)
        _login(client, "auto@x.com")
        snapshot_id = metrics_sync.latest_valid_snapshot(automation.id).id
        resp = self._post(client, {"snapshot_id": snapshot_id, "verdict": "confirmed", "comment": "ТЕСТ"})
        assert resp.status_code == 403
        assert ValueItemConfirmation.query.count() == 0

    def test_missing_csrf_is_rejected(self, app, client, automation):
        _sync(automation, _doc(_item()))
        _login(client, "admin@x.com")
        resp = client.post("/automations/test-hr/value/recruiter-hours/confirm",
                           data={"verdict": "confirmed", "comment": "ТЕСТ"})
        # The app's CSRFError handler flashes and redirects instead of a bare 400.
        assert resp.status_code == 302
        assert ValueItemConfirmation.query.count() == 0
        assert _events("value_item_confirmed") == []


# --- /value rendering and money buckets -----------------------------------------------

def _page(client, email="admin@x.com"):
    _login(client, email)
    return client.get("/value?status=all").get_data(as_text=True)


class TestValuePage:
    def test_column_items_table_and_totals(self, app, client, automation):
        _sync(automation, _doc(_item(), RUN_COST))
        html = _page(client)
        assert "Цінність, $/міс" in html
        assert "$11" in html  # net: 36 - 25
        assert "Оцінка, не доведено" in html
        assert "ТЕСТ Рекрутер не перевіряє воронку" in html and "9 год/міс × $8 × 0,5" in html
        assert "−$25" in html and "Експлуатація" in html
        assert "Разом: підтверджено $0, оцінка $11" in html
        assert "/automations/test-hr/value/recruiter-hours/confirm" in html

    def test_item_line_opens_how_it_was_counted(self, app, client, automation):
        _sync(automation, _doc(_item(), RUN_COST))
        html = _page(client)
        block = html[html.index(">Скільки дає<"):html.index(">Що змінилось у цифрах<")]
        # Below 576px a line's parts stack; from sm up they sit in one row.
        assert block.count('<summary class="d-flex flex-column flex-sm-row') == 2
        for label in ("Як пораховано", "Чому так", "Звідки дані", "обсяг з: припущення",
                      "ТЕСТ Рекрутер не перевіряє воронку", "9 год/міс × $8 × 0,5", "−$25"):
            assert label in block
        assert "table" not in block and "/confirm" not in block
        assert html.count("/value/recruiter-hours/confirm") == 1

    def test_non_admin_sees_no_confirm_button(self, app, client, automation):
        _sync(automation, _doc(_item()))
        _make_user("auto@x.com", "ТЕСТ Автоматизатор", Role.AUTOMATOR)
        html = _page(client, "auto@x.com")
        assert "ТЕСТ Рекрутер не перевіряє воронку" in html
        assert "/value/recruiter-hours/confirm" not in html

    def test_open_question_shows_who_is_asked(self, app, client, automation):
        q = {"metric_id": None, "value_item_id": "recruiter-hours", "field": "source",
             "question": "Звідки 9 год?", "ask_whom": "department_lead", "since": "2026-10-20"}
        _sync(automation, _doc(_item(source=None), questions=[q]))
        html = _page(client)
        assert "не задано, питання до: керівник відділу" in html
        assert "<li>ТЕСТ Рекрутер не перевіряє воронку: Звідки 9 год?</li>" in html

    def test_confirmed_item_status(self, app, client, automation):
        _sync(automation, _doc(_item()))
        _confirm(automation, User.query.first())
        html = _page(client)
        assert "Разом: підтверджено $36, оцінка $0" in html
        assert ">Підтверджено</span>" in html

    def test_not_applicable_baseline(self, app, client, automation):
        m = _metric(baseline={"value": None, "not_applicable_why": "ТЕСТ до запуску історії не було"})
        _sync(automation, _doc(None, metrics=[m]))
        html = _page(client)
        assert "не застосовно: ТЕСТ до запуску історії не було" in html

    def test_without_value_items_page_is_as_before(self, app, client, automation):
        _sync(automation, _doc(None))
        html = _page(client)
        assert "Цінність за статтями" not in html and "статей цінності" not in html


class TestMoneyBuckets:
    def _context(self, automation):
        return value_dashboard.build_context([automation], today=TODAY)

    def test_items_go_into_the_same_buckets(self, app, automation):
        measured = _item(id="measured-one", kind="measured", basis="data", usd_per_month=100,
                         source="ТЕСТ журнал 2026-09-01..2026-09-30")
        rejected = _item(id="rejected-one", category="losses", usd_per_month=50)
        partial = _item(id="partial-one", category="revenue", usd_per_month=20)
        _sync(automation, _doc(_item(), measured, rejected, partial, RUN_COST))
        admin = User.query.first()
        _confirm(automation, admin, "recruiter-hours", "confirmed")
        _confirm(automation, admin, "rejected-one", "rejected")
        _confirm(automation, admin, "partial-one", "partial")
        ctx = self._context(automation)
        assert ctx["money"] == {"confirmed": 36.0, "measured": 100.0, "estimate": -5.0, "rejected": 50.0}
        assert ctx["money_total"] == 131.0
        value = ctx["rows"][0]["value"]
        assert value["net"] == 131.0 and value["status"] == "estimate"

    def test_status_measured_when_nothing_is_an_estimate(self, app, automation):
        measured = _item(kind="measured", basis="data", source="ТЕСТ журнал 2026-09-01..2026-09-30")
        _sync(automation, _doc(measured))
        assert self._context(automation)["rows"][0]["value"]["status_label"] == "Виміряно, не підтверджено"

    def test_metric_money_is_not_counted_twice(self, app, automation):
        _sync(automation, _doc(_item()))
        # A stale metric.money left in the database must not add to the items.
        metric = AutomationMetric.query.one()
        metric.money = {"rate_usd_per_unit": 8, "realization": 1.0}
        db.session.commit()
        ctx = self._context(automation)
        assert ctx["money_total"] == 36.0

    def test_item_without_amount_is_not_summed(self, app, automation):
        q = {"metric_id": None, "value_item_id": "recruiter-hours", "field": "usd_per_month",
             "question": "Скільки?", "ask_whom": "automator", "since": "2026-10-20"}
        _sync(automation, _doc(_item(usd_per_month=None), RUN_COST, questions=[q]))
        value = self._context(automation)["rows"][0]["value"]
        assert value["net"] == -25.0


def test_humanized_item_error():
    msg = ("Стаття цінності «recruiter-hours»: поле formula порожнє і немає відкритого питання з "
           "value_item_id='recruiter-hours', field='formula'. Спитай автоматизатора")
    assert value_dashboard.humanize_error(msg) == (
        "Стаття цінності «recruiter-hours»: не заповнено «Як пораховано», і не записано, кого про це спитати.")


NBSP, MINUS = " ", "−"


@pytest.mark.parametrize("value,expected", [
    (0, "$0"),
    (575, "$575"),
    (575.4, "$575"),
    (1220, f"$1{NBSP}220"),
    (1219.99, f"$1{NBSP}220"),
    (1234567, f"$1{NBSP}234{NBSP}567"),
    (-40, f"{MINUS}$40"),
    (-1500.5, f"{MINUS}$1{NBSP}500"),
    (-0.4, "$0"),
    (None, "—"),
])
def test_fmt_usd(value, expected):
    assert value_dashboard.fmt_usd(value) == expected


def test_value_page_has_one_money_format(app, client, automation):
    """No cents anywhere on /value: tile, value column, totals, journal,
    subscriptions and a metric's money explanation all go through fmt_usd."""
    from src.models import Subscription
    money_metric = _metric(id="hours", name="ТЕСТ Години", type="time", unit="год/міс", primary=False,
                           baseline={"value": 10, "period": "2026-06-01..2026-06-30",
                                     "source": "ТЕСТ журнал", "kind": "measured"},
                           measurements=[{"date": "2026-09-01", "period": "2026-08-01..2026-08-31", "value": 4,
                                          "source": "ТЕСТ журнал", "kind": "measured", "note": ""}],
                           money={"rate_usd_per_unit": 8.5, "realization": 0.9, "realization_why": "ТЕСТ"})
    _sync(automation, _doc(None, metrics=[_metric(), money_metric]))
    other = Automation(slug="test-items", name="ТЕСТ Статті", owner_id=automation.owner_id)
    db.session.add(other)
    db.session.add(Subscription(name="ТЕСТ Railway", monthly_cost_usd=1219.99, automations=[automation]))
    db.session.commit()
    _sync(other, _doc(_item(usd_per_month=1220.6), RUN_COST))
    html = _page(client)
    assert ".00" not in html
    assert f"$1{NBSP}220" in html  # subscriptions tile, rounded
    assert f"$1{NBSP}221" in html  # the item
    assert f"{MINUS}$25" in html
    assert "$46/міс" in html  # metric money: 6 × $8.5 × 0.9 = 45.9


def test_migrate_value_items_is_idempotent(app):
    ValueItemConfirmation.__table__.drop(db.engine)
    AutomationValueItem.__table__.drop(db.engine)
    runner = app.test_cli_runner()
    first = runner.invoke(args=["migrate-value-items"])
    assert "created automation_value_item, value_item_confirmation" in first.output
    second = runner.invoke(args=["migrate-value-items"])
    assert "Already migrated" in second.output
