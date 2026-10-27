"""Covers src/value_dashboard.py's text helpers for /value: dates, periods,
sources with script paths, contract field names, validation errors and
journal entries - the parts that turn the metrics contract's raw values
into plain Ukrainian."""
import datetime
from types import SimpleNamespace

from src import value_dashboard as vd


def test_fmt_date():
    assert vd.fmt_date(datetime.date(2026, 9, 22)) == "22.09.2026"
    assert vd.fmt_date(datetime.datetime(2026, 9, 22, 13, 5)) == "22.09.2026"
    assert vd.fmt_date("2026-10-26") == "26.10.2026"
    assert vd.fmt_date("колись") == "колись"


def test_fmt_period():
    assert vd.fmt_period("2026-05-20..2026-07-21") == "20.05–21.07.2026"
    assert vd.fmt_period("2025-12-20..2026-01-10") == "20.12.2025–10.01.2026"
    assert vd.fmt_period("квітень 2026") == "квітень 2026"
    assert vd.fmt_period("з 2026-04-01 до кінця місяця") == "з 01.04.2026 до кінця місяця"


def test_split_source_moves_technical_parts_aside():
    source = ("scripts/roi_report.py, запуск 22.09.2026 (вікно до: стільки ж робочих днів перед "
              "запуском, скільки після, за _before_weekdays)")
    plain, tech = vd.split_source(source)
    assert plain == "запуск 22.09.2026 (вікно до: стільки ж робочих днів перед запуском, скільки після)"
    assert tech == "scripts/roi_report.py, _before_weekdays"
    assert vd.split_source("scripts/roi_report.py, кеш рухів 55 018 записів") == \
        ("кеш рухів 55 018 записів", "scripts/roi_report.py")
    assert vd.split_source("scripts/roi_report.py") == ("дані скрипта в репозиторії", "scripts/roi_report.py")
    assert vd.split_source("оцінка автоматизатора: 15 хв × 40 разів") == \
        ("оцінка автоматизатора: 15 хв × 40 разів", "")
    assert vd.split_source(None) == ("не вказано", "")


def test_field_and_ask_whom_labels():
    assert vd.field_label("target") == "Ціль"
    assert vd.field_label("check_every_days") == "Як часто міряти"
    assert vd.field_label("baseline") == "До"
    assert vd.field_label("method") == "Як перевіряємо"
    assert vd.field_label("money") == "Гроші"
    assert vd.ask_whom_label("department_lead") == "керівник відділу"
    assert vd.ask_whom_label("automator") == "автоматизатор"
    assert vd.ask_whom_label("crm_query") == "запит у CRM"
    assert vd.ask_whom_label("manual_measurement") == "ручний замір"
    assert vd.ask_whom_label("Оксана") == "Оксана"


def test_humanize_error():
    raw = ("Метрика «Воронка стоїть»: поле check_every_days порожнє і немає відкритого питання "
           "(open_questions) з metric_id='funnel-stalled', field='check_every_days'.")
    assert vd.humanize_error(raw) == \
        "«Воронка стоїть»: не заповнено «Як часто міряти», і не записано, кого про це спитати."
    assert vd.humanize_error("Головна метрика (primary: true) має бути рівно одна, зараз 0") == \
        "Не вибрано головну метрику."
    assert vd.humanize_error("Головна метрика (primary: true) має бути рівно одна, зараз 2") == \
        "Головних метрик 2, а має бути одна."
    assert vd.humanize_error("metrics.json не є валідним JSON: Expecting value, рядок 1, позиція 1") == \
        "Файл пошкоджений: його не вдалося прочитати."
    assert vd.humanize_error("щось інше") == "щось інше"


def _event(event_type, old=None, new=None, metric_key="funnel", actor_label="github sync", field=None):
    return SimpleNamespace(event_type=event_type, old_value=old, new_value=new, metric_key=metric_key,
                           actor_label=actor_label, field=field, created_at=datetime.datetime(2026, 9, 22, 10, 0))


METRICS = {"funnel": SimpleNamespace(name="Воронка стоїть", unit="% вакансіє-днів")}


def test_describe_event_texts():
    added = vd.describe_event(_event("measurement_added", new={
        "date": "2026-09-22", "period": "2026-07-22..2026-09-22", "value": 13}), METRICS)
    assert added["text"] == "Новий замір: Воронка стоїть, 13% за 22.07–22.09.2026"
    assert added["who"] == "GitHub"
    assert vd.describe_event(_event("sync_ok", new={"metrics": 4}, metric_key=None), METRICS)["text"] == \
        "Синк з GitHub: успішно"
    assert vd.describe_event(_event("sync_failed", new={"errors": ["a", "b"]}, metric_key=None), METRICS)["text"] == \
        "Синк з GitHub: файл метрик відхилено (2 помилок)"
    target = vd.describe_event(_event("target_changed", old={"value": 15, "why": "x"},
                                      new={"value": 12, "why": "y"}), METRICS)
    assert target["text"] == "Змінено ціль: 15% → 12% (Воронка стоїть)"
    every = vd.describe_event(_event("method_changed", old={"method": "m", "check_every_days": None},
                                     new={"method": "m", "check_every_days": 30}), METRICS)
    assert every["text"] == "Змінено частоту замірів: не задано → раз на 30 дн. (Воронка стоїть)"
    changed = vd.describe_event(_event("measurement_changed_in_repo",
                                       old={"date": "2026-09-22", "period": "2026-07-22..2026-09-22", "value": 13},
                                       new={"date": "2026-09-22", "period": "2026-07-22..2026-09-22", "value": 11}),
                                METRICS)
    assert changed["text"] == ("Замір змінено в репо: Воронка стоїть, за 22.07–22.09.2026: 13% → 11%. "
                               "У дашборді лишилось старе значення")
    manual = vd.describe_event(_event("roi_field_changed", old=10.0, new=12.5, metric_key=None,
                                      actor_label="Оксана", field="measured_hours_per_month"), METRICS)
    assert manual == {"when": datetime.datetime(2026, 9, 22, 10, 0), "who": "Оксана",
                      "text": "Змінено вручну: виміряно годин/міс, 10 → 12.5"}


def test_describe_event_never_shows_raw_json():
    for event_type, old, new in [
        ("baseline_changed", {"value": 28, "period": "2026-05-20..2026-07-21", "source": "s", "kind": "measured"},
         {"value": 30, "period": "2026-05-20..2026-07-21", "source": "s", "kind": "measured"}),
        ("metric_added", None, {"name": "Воронка стоїть"}),
        ("confirmed", None, {"verdict": "partial", "comment": "ок", "snapshot_id": 3}),
        ("confirmation_reset", {"verdict": "confirmed", "confirmation_id": 1}, {"reason": "новий замір"}),
    ]:
        text = vd.describe_event(_event(event_type, old=old, new=new), METRICS)["text"]
        assert "{" not in text and "snapshot_id" not in text and "confirmation_id" not in text, text
