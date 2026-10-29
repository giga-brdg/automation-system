"""Full-scale estimates stay separate from current value. All data is TEST DATA."""
import pytest
from test_value_items import TODAY, _doc, _item, _page, _sync
from test_value_items import automation as _automation_fixture

from src import value_dashboard

automation = _automation_fixture

NBSP = "\u00a0"
AT_SCALE = {
    "usd_per_month_low": 4976,
    "usd_per_month_high": 5989,
    "volume": "ТЕСТ 500–600 контактів/день",
    "condition": "ТЕСТ 20–24 прогріті ящики та база контактів",
    "formula": "ТЕСТ та сама формула на обсязі ТЗ, мінус сервіси",
    "source": "ТЕСТ ТЗ Максима",
    "as_of": "2026-10-28",
}


def scale_doc(**overrides):
    data = _doc(_item(usd_per_month=932))
    data["at_scale"] = dict(AT_SCALE, **overrides)
    return data


def test_full_scale_renders_under_current_value_and_in_expanded_card(app, client, automation):
    assert _sync(automation, scale_doc()) == []
    html = _page(client)
    assert f"на повному масштабі ~$4{NBSP}976–$5{NBSP}989" in html
    assert 'title="Обсяг: ТЕСТ 500–600 контактів/день' in html
    assert 'class="at-scale-block' in html
    block = html.split('class="at-scale-block', 1)[1].split('<!-- /at-scale -->', 1)[0]
    for text in ("Повний масштаб", "оцінка, у суми не входить", AT_SCALE["volume"],
                 AT_SCALE["condition"], AT_SCALE["formula"], AT_SCALE["source"], "28.10.2026"):
        assert text in block
    assert ".00" not in block
    assert ">$932<" in html


def test_full_scale_tile_uses_lower_bounds_without_adding_to_current_money(app, client, automation):
    from src.extensions import db
    from src.models import Automation

    _sync(automation, scale_doc())
    other = Automation(slug="test-second", name="ТЕСТ Друга", owner_id=automation.owner_id)
    db.session.add(other)
    db.session.commit()
    _sync(other, scale_doc(usd_per_month_low=1000, usd_per_month_high=2000))
    ctx = value_dashboard.build_context([automation, other], today=TODAY)
    assert ctx["at_scale_total"] == 5976
    assert ctx["at_scale_n"] == 2
    assert ctx["money_total"] == 1864
    assert ctx["money"]["estimate"] == 1864
    assert ctx["rows"][0]["value"]["net"] == 932
    html = _page(client)
    assert f"потенціал на повному масштабі: ~$5{NBSP}976" in html
    assert f">$1{NBSP}864<" in html


@pytest.mark.parametrize("high", [None, 4976])
def test_single_figure_has_no_range(app, client, automation, high):
    _sync(automation, scale_doc(usd_per_month_high=high))
    html = _page(client)
    assert f"на повному масштабі ~$4{NBSP}976" in html
    assert f"~$4{NBSP}976–" not in html


@pytest.mark.parametrize("explicit_null", [False, True])
def test_no_full_scale_preserves_existing_page(app, client, automation, explicit_null):
    data = _doc(_item(usd_per_month=932))
    if explicit_null:
        data["at_scale"] = None
    _sync(automation, data)
    ctx = value_dashboard.build_context([automation], today=TODAY)
    assert ctx["at_scale_total"] is None and ctx["at_scale_n"] == 0
    assert ctx["money_total"] == 932
    html = _page(client)
    assert "на повному масштабі" not in html and "at-scale-block" not in html


def test_unknown_full_scale_amount_is_not_shown_as_zero(app, client, automation):
    data = scale_doc(usd_per_month_low=None, usd_per_month_high=None, volume=None, as_of=None)
    data["open_questions"] = [{
        "metric_id": None, "value_item_id": None, "field": "at_scale",
        "question": "ТЕСТ Який обсяг і сума?", "ask_whom": "department_lead", "since": "2026-10-28",
    }]
    assert _sync(automation, data) == []
    ctx = value_dashboard.build_context([automation], today=TODAY)
    assert ctx["at_scale_total"] is None
    html = _page(client)
    block = html.split('class="at-scale-block', 1)[1].split('<!-- /at-scale -->', 1)[0]
    assert "не задано, питання до: керівник відділу" in block
    assert "~$0" not in block
    assert "Повний масштаб: ТЕСТ Який обсяг і сума?" in html
    assert "потенціал на повному масштабі:" not in html


def test_invalid_snapshot_keeps_last_accepted_full_scale(app, client, automation):
    _sync(automation, scale_doc())
    warnings = _sync(automation, scale_doc(usd_per_month_high=100), sha="b" * 40)
    assert warnings
    ctx = value_dashboard.build_context([automation], today=TODAY)
    assert ctx["at_scale_total"] == 4976
    assert ctx["money_total"] == 932
    html = _page(client)
    assert "Останній синк файлу метрик відхилено" in html
    assert f"на повному масштабі ~$4{NBSP}976–$5{NBSP}989" in html
    _sync(automation, _doc(_item(usd_per_month=932)), sha="c" * 40)
    assert value_dashboard.build_context([automation], today=TODAY)["at_scale_total"] is None


def test_full_scale_text_and_tooltip_are_escaped(app, client, automation):
    payload = '<script>alert("TEST")</script>'
    _sync(automation, scale_doc(volume=payload, condition=payload, formula=payload, source=payload))
    html = _page(client)
    assert payload not in html
    assert "&lt;script&gt;alert" in html


def test_full_scale_page_supports_strict_undefined(app, client, automation):
    from jinja2 import StrictUndefined

    app.jinja_env.undefined = StrictUndefined
    _sync(automation, scale_doc())
    assert "Повний масштаб" in _page(client)


@pytest.mark.parametrize("source", [123, {"TEST": "source"}])
def test_malformed_scale_source_cannot_break_portfolio(app, client, automation, source):
    from src.metrics_contract import validate

    data = scale_doc(source=source)
    assert validate(data, today=TODAY)[0] == []  # Existing reference contract remains unchanged.
    _sync(automation, data)
    html = _page(client)
    assert "ТЕСТ HR" in html and "Повний масштаб" in html
    assert "Перевірте дані повного масштабу" in html


@pytest.mark.parametrize("field,value", [
    ("usd_per_month_low", float("inf")),
    ("usd_per_month_low", float("nan")),
    ("usd_per_month_low", 10**400),
    ("usd_per_month_high", float("inf")),
    ("usd_per_month_high", float("nan")),
    ("usd_per_month_high", 10**400),
])
def test_unsafe_scale_amount_cannot_break_portfolio(app, client, automation, field, value):
    data = scale_doc(usd_per_month_high=None)
    data["at_scale"][field] = value
    assert _sync(automation, data) == []
    html = _page(client)
    assert "ТЕСТ HR" in html
    assert "Перевірте дані повного масштабу" in html
    ctx = value_dashboard.build_context([automation], today=TODAY)
    assert ctx["at_scale_total"] is None
    assert ctx["money_total"] == 932


def test_scale_total_overflow_cannot_break_portfolio(app, client, automation):
    from src.extensions import db
    from src.models import Automation

    other = Automation(slug="test-overflow", name="ТЕСТ Велика сума", owner_id=automation.owner_id)
    db.session.add(other)
    db.session.commit()
    for a in (automation, other):
        _sync(a, scale_doc(usd_per_month_low=1e308, usd_per_month_high=None))
    ctx = value_dashboard.build_context([automation, other], today=TODAY)
    assert ctx["at_scale_total"] is None
    assert "ТЕСТ HR" in _page(client)


@pytest.mark.parametrize("with_scale", [False, True])
def test_numeric_general_question_does_not_break_legacy_or_scale_page(app, client, automation, with_scale):
    data = scale_doc() if with_scale else _doc(_item())
    data["open_questions"] = [{"metric_id": None, "value_item_id": None, "field": 42,
                               "question": "ТЕСТ Загальне питання", "ask_whom": "automator",
                               "since": "2026-10-28"}]
    assert _sync(automation, data) == []
    html = _page(client)
    assert "ТЕСТ HR" in html and "Загальне: ТЕСТ Загальне питання" in html


def test_missing_scale_date_names_the_question_owner(app, client, automation):
    data = scale_doc(as_of=None)
    data["open_questions"] = [{"metric_id": None, "value_item_id": None, "field": "at_scale.as_of",
                               "question": "ТЕСТ Коли звірено?", "ask_whom": "department_lead",
                               "since": "2026-10-28"}]
    assert _sync(automation, data) == []
    html = _page(client)
    assert 'Станом на: <span class="text-muted">не задано, питання до: керівник відділу</span>' in html
