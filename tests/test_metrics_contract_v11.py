"""Contract v1.1 in src/metrics_contract.py: value_items, value_item_id,
baseline.not_applicable_why, at_scale, the "_приклад" key and ROI.md's value
summary.

The cases are the ones from stage-1-supplax's tests/test_check_metrics.py.
Each one asserts its own expected message, so it runs anywhere; when the
skill's scripts/check_metrics.py can be found (SKILL_CHECK_METRICS env var,
or the usual install locations) every case also runs through it and the
two validators must return exactly the same errors and warnings."""
import datetime
import importlib.util
import os
import pathlib

import pytest

from src import metrics_contract

TODAY = datetime.date(2026, 8, 1)


def metric(**overrides):
    m = {
        "id": "test-metric", "name": "Тестова метрика", "type": "quality", "unit": "%",
        "direction": "down", "primary": True,
        "baseline": {"value": 28, "period": "2026-04-01..2026-07-21",
                     "source": "тестове джерело", "kind": "measured"},
        "target": {"value": 15, "why": "тестове значення"},
        "method": "тестовий метод", "check_every_days": 30, "measurements": [], "money": None,
    }
    m.update(overrides)
    return m


def doc(*metrics, **overrides):
    d = {"version": 1, "launched_at": "2026-07-22",
         "metrics": list(metrics) or [metric()], "open_questions": []}
    d.update(overrides)
    return d


def item(**overrides):
    x = {
        "id": "recruiter-hours", "category": "time",
        "title": "Рекрутер не перевіряє воронку вручну", "usd_per_month": 36,
        "kind": "estimate", "basis": "assumption", "formula": "9 год/міс × $8 × 0,5",
        "why": "0,5: звільнений час іде на дзвінки", "source": "оцінка автоматизатора",
        "as_of": "2026-07-30", "metric_ids": ["test-metric"],
    }
    x.update(overrides)
    return x


RUN_COST = {"id": "run-cost", "category": "run_cost", "title": "Хостинг і підтримка",
            "usd_per_month": -25, "kind": "estimate", "basis": "data",
            "formula": "Railway $10 + 1 год/міс × $15", "why": "рахунок за липень",
            "source": "рахунок Railway", "as_of": "2026-07-30", "metric_ids": None}


def value_doc(*items, **overrides):
    return doc(value_items=list(items) or [item()], **overrides)


def question(**overrides):
    q = {"metric_id": None, "value_item_id": "recruiter-hours", "field": "formula",
         "question": "?", "ask_whom": "automator", "since": "2026-07-30"}
    q.update(overrides)
    return q


# v1.1 at_scale: the "повний масштаб" block, the cold-email example from the
# contract. Never summed with value_items, so it never carries a `kind`.
AT_SCALE = {
    "usd_per_month_low": 4976,
    "usd_per_month_high": 5989,
    "volume": "500–600 контактів/день (11–13 тис./міс)",
    "condition": "11–13 тис. нових контактів/міс, 20–24 прогріті ящики, людина на відповіді",
    "formula": "та сама формула, що в статтях цінності, на обсязі ТЗ, мінус сервіси $264–299",
    "source": "ТЗ Максима 05.10.2026; CRM-когорта colddesigner",
    "as_of": "2026-10-28",
}


def scale_doc(at_scale=None, **overrides):
    return value_doc(at_scale=AT_SCALE if at_scale is None else at_scale, **overrides)


def general_question(**overrides):
    """A question about at_scale: both ids null, field "at_scale.<field>"."""
    q = {"metric_id": None, "value_item_id": None, "field": "at_scale",
         "question": "Який цільовий обсяг погоджено?", "ask_whom": "automator",
         "since": "2026-10-28"}
    q.update(overrides)
    return q


MEASURED_OK = item(kind="measured", basis="data", source="журнал часу за 2026-06-01..2026-06-30")
MONEY_METRIC = metric(money={"rate_usd_per_unit": 8, "realization": 0.5, "realization_why": "переключення"})
GOOD_ROI_MD = ("## Confidence\nEstimated\n\n## Actual Results\n<!-- c -->\n"
               "Заміряно $0/міс, оцінка $11/міс\nЗа категоріями: Час $36, Експлуатація -$25\n")

# (name, data, roi_md, expected error substring or None for "no errors",
#  expected warning substring or None for "no warnings")
CASES = [
    ("v1 file without value_items", doc(), None, None, None),
    ("valid value items", value_doc(item(), RUN_COST), None, None, None),
    ("unknown category", value_doc(item(category="profit")), None, "category", None),
    ("time negative", value_doc(item(usd_per_month=-36)), None, "usd_per_month", None),
    ("time zero", value_doc(item(usd_per_month=0)), None, "usd_per_month", None),
    ("revenue negative", value_doc(item(category="revenue", usd_per_month=-1)), None, "usd_per_month", None),
    ("losses zero", value_doc(item(category="losses", usd_per_month=0)), None, "usd_per_month", None),
    ("run_cost positive", value_doc(item(category="run_cost", usd_per_month=25)), None, "від'ємним", None),
    ("run_cost zero", value_doc(dict(RUN_COST, usd_per_month=0)), None, None, None),
    ("usd as string", value_doc(item(usd_per_month="36")), None, "числом", None),
    ("empty formula", value_doc(item(formula="")), None, "поле formula порожнє", None),
    ("empty why", value_doc(item(why="")), None, "поле why порожнє", None),
    ("empty source", value_doc(item(source="")), None, "поле source порожнє", None),
    ("null usd", value_doc(item(usd_per_month=None)), None, "поле usd_per_month порожнє", None),
    ("null formula with question", value_doc(item(formula=None), open_questions=[question()]), None, None, None),
    ("null usd with question",
     value_doc(item(usd_per_month=None), open_questions=[question(field="usd_per_month")]), None, None, None),
    ("metric question doesn't cover item",
     value_doc(item(formula=None), open_questions=[question(metric_id="recruiter-hours", value_item_id=None)]),
     None, "поле formula порожнє", "посилається на метрику"),
    ("duplicate item ids", value_doc(item(), item(title="Інше")), None, "повторюється", None),
    ("item id not kebab", value_doc(item(id="Recruiter_Hours")), None, "kebab-case", None),
    ("bad kind", value_doc(item(kind="guess")), None, "kind має бути", None),
    ("bad basis", value_doc(item(basis="gut")), None, "basis", None),
    ("bad as_of", value_doc(item(as_of="30.07.2026")), None, "as_of", None),
    ("confirmed kind", value_doc(item(kind="confirmed")), None, "тільки дашборд", None),
    ("measured on assumption", value_doc(item(kind="measured", basis="assumption")), None, "basis: data", None),
    ("measured without period", value_doc(item(kind="measured", basis="data")), None,
     "YYYY-MM-DD..YYYY-MM-DD", None),
    ("measured with period", value_doc(MEASURED_OK), None, None, None),
    ("measured reversed period",
     value_doc(dict(MEASURED_OK, source="журнал за 2026-06-30..2026-06-01")), None, "YYYY-MM-DD..YYYY-MM-DD", None),
    ("money plus value items", value_doc(item(), metrics=[MONEY_METRIC]), None, "в одному місці", None),
    ("money alone", doc(MONEY_METRIC), None, None, None),
    ("money with empty value items", doc(MONEY_METRIC, value_items=[]), None, None, None),
    ("unknown metric link", value_doc(item(metric_ids=["nope"])), None, None, "'nope'"),
    ("metric_ids not a list", value_doc(item(metric_ids="test-metric")), None, "metric_ids", None),
    ("value_items not a list", doc(value_items={}), None, "value_items має бути списком", None),
    ("missing item key", value_doc({k: v for k, v in item().items() if k != "as_of"}), None, "бракує", None),
    ("question with both ids",
     value_doc(open_questions=[question(metric_id="test-metric")]), None, "рівно", None),
    ("question to unknown item", value_doc(open_questions=[question(value_item_id="ghost")]), None, None, "'ghost'"),
    ("general question", value_doc(open_questions=[question(value_item_id=None, field="launched_at")]),
     None, None, None),
    ("baseline not applicable",
     doc(metric(baseline={"value": None, "not_applicable_why": "до запуску історії не було"})), None, None, None),
    ("baseline not applicable blank",
     doc(metric(baseline={"value": None, "not_applicable_why": " "})), None, "поле baseline порожнє", None),
    ("baseline value and not applicable",
     doc(metric(baseline={"value": 28, "period": "квітень", "source": "CRM", "kind": "estimate",
                          "not_applicable_why": "неможливо"})), None, "not_applicable_why", None),
    ("template example key", dict(doc(), _приклад={}), None, "_приклад", None),
    ("ROI.md value summary ok", value_doc(item(), RUN_COST), GOOD_ROI_MD, None, None),
    ("ROI.md value summary wrong", value_doc(item(), RUN_COST),
     "## Actual Results\nПідтверджено $36\n", "Actual Results", None),
    ("ROI.md without value items", doc(), "## Actual Results\n\n", None, None),
    # at_scale
    ("at_scale absent", value_doc(), None, None, None),
    ("at_scale null", value_doc(at_scale=None), None, None, None),
    ("at_scale valid", scale_doc(), None, None, None),
    ("at_scale without value items", doc(at_scale=AT_SCALE), None, "спочатку статті цінності", None),
    ("at_scale with empty value items", doc(at_scale=AT_SCALE, value_items=[]), None,
     "спочатку статті цінності", None),
    ("at_scale not an object", value_doc(at_scale=4976), None, "at_scale має бути об'єктом", None),
    ("at_scale missing volume key", scale_doc({k: v for k, v in AT_SCALE.items() if k != "volume"}),
     None, "бракує обов'язкових полів volume", None),
    ("at_scale high equal to low", scale_doc(dict(AT_SCALE, usd_per_month_high=4976)), None, None, None),
    ("at_scale high null", scale_doc(dict(AT_SCALE, usd_per_month_high=None)), None, None, None),
    ("at_scale without high key",
     scale_doc({k: v for k, v in AT_SCALE.items() if k != "usd_per_month_high"}), None, None, None),
    ("at_scale low zero", scale_doc(dict(AT_SCALE, usd_per_month_low=0, usd_per_month_high=None)),
     None, "usd_per_month_low має бути більше 0", None),
    ("at_scale low negative",
     scale_doc(dict(AT_SCALE, usd_per_month_low=-100, usd_per_month_high=None)), None,
     "usd_per_month_low має бути більше 0", None),
    ("at_scale low as string", scale_doc(dict(AT_SCALE, usd_per_month_low="4976")), None,
     "usd_per_month_low має бути числом", None),
    ("at_scale high as string", scale_doc(dict(AT_SCALE, usd_per_month_high="5989")), None,
     "usd_per_month_high має бути числом", None),
    ("at_scale high below low", scale_doc(dict(AT_SCALE, usd_per_month_high=4000)), None,
     "менше за usd_per_month_low", None),
    ("at_scale empty low", scale_doc(dict(AT_SCALE, usd_per_month_low=None)), None,
     "поле at_scale.usd_per_month_low порожнє", None),
    ("at_scale empty volume", scale_doc(dict(AT_SCALE, volume="")), None,
     "поле at_scale.volume порожнє", None),
    ("at_scale empty condition", scale_doc(dict(AT_SCALE, condition="")), None,
     "поле at_scale.condition порожнє", None),
    ("at_scale empty formula", scale_doc(dict(AT_SCALE, formula="")), None,
     "поле at_scale.formula порожнє", None),
    ("at_scale empty source", scale_doc(dict(AT_SCALE, source="")), None,
     "поле at_scale.source порожнє", None),
    ("at_scale empty as_of", scale_doc(dict(AT_SCALE, as_of="")), None,
     "поле at_scale.as_of порожнє", None),
    ("at_scale empty field with its own question",
     scale_doc(dict(AT_SCALE, volume=None), open_questions=[general_question(field="at_scale.volume")]),
     None, None, None),
    ("at_scale question covers every field",
     scale_doc(dict(AT_SCALE, volume=None, condition=None, usd_per_month_low=None),
               open_questions=[general_question()]), None, None, None),
    ("metric question doesn't cover at_scale",
     scale_doc(dict(AT_SCALE, volume=None),
               open_questions=[{"metric_id": "test-metric", "field": "at_scale.volume",
                                "question": "?", "ask_whom": "automator", "since": "2026-10-28"}]),
     None, "поле at_scale.volume порожнє", None),
    ("at_scale bad as_of", scale_doc(dict(AT_SCALE, as_of="28.10.2026")), None,
     "as_of має бути датою", None),
    ("at_scale with kind key", scale_doc(dict(AT_SCALE, kind="estimate")), None, None,
     "завжди оцінка"),
]


@pytest.mark.parametrize("name,data,roi_md,error,warning", CASES, ids=[c[0] for c in CASES])
def test_contract_case(name, data, roi_md, error, warning):
    errors, warnings = metrics_contract.validate(data, today=TODAY, roi_md=roi_md)
    if error is None:
        assert errors == []
    else:
        assert any(error in e for e in errors), errors
    if warning is None:
        assert warnings == []
    else:
        assert any(warning in w for w in warnings), warnings


def test_value_summary_matches_skill_format():
    revenue = item(id="more-finalists", category="revenue", usd_per_month=1348)
    summary = metrics_contract.value_summary(value_doc(MEASURED_OK, revenue, RUN_COST))
    assert summary == ("Заміряно $36/міс, оцінка $1 323/міс",
                       "За категоріями: Дохід $1 348, Час $36, Експлуатація -$25")
    assert metrics_contract.value_summary(doc()) is None


def test_at_scale_is_never_part_of_the_value_summary():
    """Current money and money at the target volume are never added together,
    so the ROI.md summary lines are the same with and without at_scale."""
    assert metrics_contract.value_summary(scale_doc()) == metrics_contract.value_summary(value_doc())
    assert "4 976" not in " ".join(metrics_contract.value_summary(scale_doc()))
    roi_md = ("## Actual Results\nЗаміряно $0/міс, оцінка $36/міс\nЗа категоріями: Час $36\n")
    assert metrics_contract.validate(scale_doc(), today=TODAY, roi_md=roi_md)[0] == []


def test_missing_metric_field_message_points_at_not_applicable_why():
    errors, _ = metrics_contract.validate(doc(metric(baseline=None)), today=TODAY)
    assert any("baseline.not_applicable_why" in e for e in errors)


# --- Parity with the skill's validator --------------------------------------

def _skill_checker():
    candidates = [os.environ.get("SKILL_CHECK_METRICS")]
    home = pathlib.Path.home()
    candidates += [
        str(home / "OneDrive/Desktop/SkillsSupplax/.claude/skills/stage-1-supplax/scripts/check_metrics.py"),
        str(home / ".claude/skills/stage-1-supplax/scripts/check_metrics.py"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("skill_check_metrics", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "value_summary"):  # v1.1 or later
                return module
    return None


SKILL = _skill_checker()


@pytest.mark.skipif(SKILL is None, reason="stage-1-supplax's check_metrics.py (v1.1) not found")
@pytest.mark.parametrize("name,data,roi_md", [c[:3] for c in CASES], ids=[c[0] for c in CASES])
def test_same_result_as_skill(name, data, roi_md):
    errors, warnings = metrics_contract.validate(data, today=TODAY, roi_md=roi_md)
    report = SKILL.check(data, today=TODAY, roi_md=roi_md)
    assert errors == report.errors
    assert warnings == report.warnings
