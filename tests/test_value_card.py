"""The expanded row on /value: three blocks by default (what it brings, what
moved in the numbers, what's still to prove) and everything else behind
"Подробиці". All data here is TEST DATA."""
import datetime
import decimal
from types import SimpleNamespace

import pytest

from test_value_items import RUN_COST, TODAY, _confirm, _doc, _item, _metric, _page, _sync, automation  # noqa: F401

from src import value_dashboard
from src.extensions import db
from src.models import Automation, ROIEntry, User

DETAILS = '<summary class="text-primary" style="cursor: pointer;">Подробиці</summary>'
NBSP, MINUS = " ", "−"


def _measured(date, value, kind="measured"):
    return {"date": date, "period": "2026-07-22..2026-10-01", "value": value,
            "source": "ТЕСТ scripts/count_stuck.py", "kind": kind, "note": ""}


def _fresh_metric(id, name, direction, before, after, primary=False, date="2026-10-20", **kw):
    return _metric(id=id, name=name, direction=direction, primary=primary,
                   baseline={"value": before, "period": "2026-04-01..2026-07-21", "source": "ТЕСТ звіт",
                             "kind": "measured"},
                   check_every_days=30, measurements=[_measured(date, after)], **kw)


def _split(html):
    """(default part, "Подробиці" part) of the one expanded row on the page."""
    start = html.index("<details>", html.index('colspan="6"'))
    more = html.index(DETAILS, start)
    end = html.index("Підписки</h5>", more)
    return html[start:more], html[more:end]


def _row():
    return value_dashboard.build_context(Automation.query.all(), today=TODAY)["rows"][0]


# --- Default view ------------------------------------------------------------------

def test_default_view_hides_method_history_verdict_paths_and_commits(app, client, automation):
    metrics = [_fresh_metric("stuck-on-stage", "ТЕСТ Застрягли", "down", 61, 57, primary=True,
                             target={"value": 12, "why": "ТЕСТ ціль"})]
    item = _item(source="ТЕСТ scripts/outcome_tail.py, кеш від 2026-10-27")
    _sync(automation, _doc(item, RUN_COST, metrics=metrics))
    default, more = _split(_page(client))
    for text in ("Як перевіряємо", "Історія замірів", "Вердикт щодо", ".py", "коміт", "id recruiter-hours"):
        assert text not in default, text
    assert "Скільки дає" in default and "Що змінилось у цифрах" in default and "Щоб довести" in default
    # Everything taken out of the default view is under "Подробиці".
    for text in ("Як перевіряємо", "Історія замірів", "scripts/outcome_tail.py", "count_stuck.py",
                 "Дані з коміту aaaaaaa", "id recruiter-hours", "id stuck-on-stage", "Журнал",
                 "/automations/test-hr/metrics/stuck-on-stage/confirm",
                 "/automations/test-hr/value/recruiter-hours/confirm", "Разом: підтверджено"):
        assert text in more, text
    assert more.count(">Підтвердити</button>") == 1  # one metric
    assert more.count(">Підтвердити</summary>") == 2  # two items


def test_money_headline_estimate(app, client, automation):
    _sync(automation, _doc(_item(), RUN_COST))
    default, _ = _split(_page(client))
    assert "≈ $11/міс, оцінка, не доведено" in default
    assert "підтверджено $0" in default


def test_money_headline_confirmed(app, client, automation):
    _sync(automation, _doc(_item()))
    _confirm(automation, User.query.first())
    default, _ = _split(_page(client))
    assert "$36/міс, підтверджено" in default and "≈" not in default


def test_money_headline_without_items_or_money(app, client, automation):
    _sync(automation, _doc(None))
    default, _ = _split(_page(client))
    assert "Грошей ще не рахували" in default


# --- Block 2 -----------------------------------------------------------------------

def test_fresh_block_keeps_three_measured_metrics_primary_first(app, automation):
    metrics = [
        _fresh_metric("up-bad", "ТЕСТ Росте погано", "up", 40, 30),
        _fresh_metric("up-good", "ТЕСТ Росте добре", "up", 10, 20),
        _fresh_metric("small", "ТЕСТ Ледь змінилось", "down", 11.9, 12.8),
        _fresh_metric("stale", "ТЕСТ Давній замір", "down", 50, 10, date="2026-08-01"),
        _metric(id="estimate-only", name="ТЕСТ Лише оцінка", primary=False,
                measurements=[_measured("2026-10-20", 5, kind="estimate")]),
        _fresh_metric("main", "ТЕСТ Головна", "down", 61, 57, primary=True),
    ]
    _sync(automation, _doc(None, metrics=metrics))
    lines = _row()["fresh"]["lines"]
    assert [m["name"] for m in lines] == ["ТЕСТ Головна", "ТЕСТ Росте погано", "ТЕСТ Росте добре"]
    main, bad, good = lines
    assert (main["before_text"], main["after_text"], main["arrow"], main["tone"]) == ("61%", "57%", "down", "success")
    assert (bad["arrow"], bad["tone"]) == ("down", "danger")
    assert (good["arrow"], good["tone"]) == ("up", "success")
    assert main["target_text"] == "ціль 15%" and main["date_text"] == "20.10.2026"


def test_stale_measurement_with_newer_estimate_is_not_fresh(app, automation):
    m = _fresh_metric("main", "ТЕСТ Головна", "down", 61, 57, primary=True, date="2026-08-01")
    m["measurements"].append(_measured("2026-10-20", 50, kind="estimate"))
    _sync(automation, _doc(None, metrics=[m]))
    assert _row()["fresh"]["lines"] == []


def _entry(direction, before, after, unit="%", target=None):
    metric = SimpleNamespace(name="ТЕСТ", unit=unit, direction=direction, is_primary=False)
    return {"metric": metric, "baseline": before, "latest_measured": after, "target_value": target,
            "latest_measured_date": datetime.date(2026, 10, 20)}


@pytest.mark.parametrize("direction,before,after,arrow,tone", [
    ("down", 61, 57, "down", "success"),
    ("down", 57, 61, "up", "danger"),
    ("up", 10, 20, "up", "success"),
    ("up", 20, 10, "down", "danger"),
    ("down", 11.9, 12.8, "up", "muted"),  # moved by less than 1
    ("up", 5, 5, "right", "muted"),
    ("up", None, 5, "right", "muted"),  # no "before"
])
def test_fresh_line_arrow_and_tone(direction, before, after, arrow, tone):
    line = value_dashboard._fresh_line(_entry(direction, before, after))
    assert (line["arrow"], line["tone"]) == (arrow, tone)


def test_fresh_line_texts():
    line = value_dashboard._fresh_line(_entry("down", 11.9, 12.8, unit="днів (медіана)"))
    assert (line["before_text"], line["after_text"], line["target_text"]) == ("11,9", "12,8 днів (медіана)", "ціль ?")
    line = value_dashboard._fresh_line(_entry("up", None, 57, target=70))
    assert (line["before_text"], line["after_text"], line["target_text"]) == ("?", "57%", "ціль 70%")


def test_fresh_line_renders_with_colour(app, client, automation):
    _sync(automation, _doc(None, metrics=[_fresh_metric("main", "ТЕСТ Головна", "down", 61, 57, primary=True)]))
    default, _ = _split(_page(client))
    assert '61% → <span class="fw-semibold text-success">57%</span> <i class="ti ti-arrow-down text-success"' in default
    assert "ціль 15%" in default and "замір 20.10.2026" in default


def test_no_fresh_measurement_says_when_the_next_is_due(app, client, automation):
    _sync(automation, _doc(None, metrics=[_metric(check_every_days=120)]))
    default, more = _split(_page(client))
    assert "Свіжих замірів немає, наступний до 19.11.2026" in default  # launch 22.07 + 120 days
    assert "ТЕСТ Заявки, що застрягли" in more


def test_overdue_says_since_when(app, automation):
    _sync(automation, _doc(None, metrics=[_metric(check_every_days=30)]))
    assert _row()["fresh"]["empty_text"] == "Свіжих замірів немає, замір прострочено з 21.08.2026"


# --- Block 3 -----------------------------------------------------------------------

def _q(whom, question, metric_id=None, item_id=None):
    return {"metric_id": metric_id, "value_item_id": item_id, "field": "target", "question": question,
            "ask_whom": whom, "since": "2026-10-27"}


def test_questions_grouped_by_whom_biggest_first(app, client, automation):
    questions = [_q("automator", "ТЕСТ Скільки коштує хостинг?", item_id="run-cost"),
                 _q("department_lead", "ТЕСТ Яка ціль?", metric_id="stuck-on-stage"),
                 _q("Оксана", "ТЕСТ Коли підтвердить?"),
                 _q("department_lead", "ТЕСТ Як часто?", metric_id="stuck-on-stage")]
    _sync(automation, _doc(_item(), RUN_COST, questions=questions))
    groups = _row()["question_groups"]
    assert [(g["who"], g["count"]) for g in groups] == [("керівник відділу", 2), ("автоматизатор", 1), ("Оксана", 1)]
    default, _ = _split(_page(client))
    assert "Відкритих питань: 4 (керівник відділу 2, автоматизатор 1, Оксана 1)" in default
    assert "<li>ТЕСТ Заявки, що застрягли: ТЕСТ Яка ціль?</li>" in default
    assert "<li>ТЕСТ Хостинг і підтримка: ТЕСТ Скільки коштує хостинг?</li>" in default
    assert "<li>Загальне: ТЕСТ Коли підтвердить?</li>" in default


def test_no_questions(app, client, automation):
    _sync(automation, _doc(_item()))
    default, _ = _split(_page(client))
    assert "Відкритих питань немає" in default


# --- "Подробиці" ---------------------------------------------------------------------

def test_journal_shows_five_and_hides_the_rest(app, client, automation):
    for n in range(4):
        _sync(automation, _doc(_item(usd_per_month=36 + n)), sha=f"{n}" * 40)
    row = _row()
    assert len(row["events"]) > 5
    _, more = _split(_page(client))
    journal = more[more.index(">Журнал<"):]
    shown, rest = journal.split(">ще</summary>")
    assert shown.count("<li>") == 5
    assert rest.count("<li>") == len(row["events"]) - 5


# --- Number formats ------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(12.8, "12,8"), (61, "61"), (0.25, "0,25"), (None, "немає даних"),
                                            (decimal.Decimal("8.00"), "8"), (decimal.Decimal("0.50"), "0,5")])
def test_fmt_number_decimal_comma(value, expected):
    assert value_dashboard.fmt_number(value) == expected


@pytest.mark.parametrize("value,signed,expected", [
    (5, False, "5,0"), (12.84, False, "12,8"), (3.5, True, "+3,5"), (-2, True, f"{MINUS}2,0")])
def test_fmt_hours(value, signed, expected):
    assert value_dashboard.fmt_hours(value, signed=signed) == expected


def test_card_has_no_cents_and_whole_dollar_money(app, client, automation):
    automation.roi = ROIEntry(dev_hours=8, maintenance_hours_per_month=0.5)  # Numeric -> Decimal("8.00")
    db.session.commit()
    _sync(automation, _doc(_item(usd_per_month=1220.4), RUN_COST))
    html = _page(client)
    assert "8 г /" in html and "0,5 г/міс" in html
    assert ".00" not in html
    default, _ = _split(html)
    assert f"≈ $1{NBSP}195/міс" in default and f"$1{NBSP}220" in default and f"{MINUS}$25" in default
