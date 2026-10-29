"""Validation of an automation repo's dashboard/metrics.json against the v1.1
contract (docs/metrics-contract.md; v1.1 only adds value_items,
OpenQuestion.value_item_id, baseline.not_applicable_why and at_scale, so a v1
file validates as before). The rules and their messages mirror
stage-1-supplax's scripts/check_metrics.py (the reference validator the
automators run before pushing) so a file that passes there passes here and
the other way round - but this module doesn't import or depend on the skill.

The rules that read docs/roi_explained.md are skipped - matching metric names
against its "## Метрики", its "## Цінність" table against value_items, its
"## Повний масштаб" against at_scale, and the v1 leniency for a field
explained only in its "## Caveats" - since the sync never fetches that file.
The append-only rule isn't a validation error here either - the dashboard
enforces it on its own stored measurements instead (src/metrics_sync.py),
so a measurement changed in the repo is kept and logged, not rejected.

Nothing here touches the database: parse_and_validate() takes text and
returns (data, errors, warnings)."""
import datetime
import json
import re

TYPES = {"time", "revenue", "cost", "quality", "conversion", "other"}
DIRECTIONS = {"down", "up"}
KINDS = {"measured", "estimate"}
METRIC_KEYS = ["id", "name", "type", "unit", "direction", "primary", "baseline",
               "target", "method", "check_every_days", "measurements"]
MEASUREMENT_KEYS = ["date", "period", "value", "source", "kind", "note"]
OPEN_QUESTION_KEYS = ["metric_id", "field", "question", "ask_whom", "since"]
# v1.1: category -> its Ukrainian name, in display order. revenue/cost_savings/
# time/losses are Оксана's A/B/C/D; run_cost is what the automation itself
# costs to run, the only negative one.
CATEGORIES = {"revenue": "Дохід", "cost_savings": "Економія витрат", "time": "Час",
              "losses": "Менше втрат", "run_cost": "Експлуатація"}
BASES = {"data", "assumption"}
VALUE_ITEM_KEYS = ["id", "category", "title", "usd_per_month", "kind", "basis",
                   "formula", "why", "source", "as_of"]
# v1.1 `at_scale`: what the automation would be worth at the agreed target
# volume, while it still runs on a small one. Always an estimate, never summed
# with value_items, so it has no `kind`. usd_per_month_high is the only
# optional field (a single figure instead of a range).
AT_SCALE_KEYS = ["usd_per_month_low", "usd_per_month_high", "volume", "condition",
                 "formula", "source", "as_of"]
AT_SCALE_REQUIRED = [k for k in AT_SCALE_KEYS if k != "usd_per_month_high"]
KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PERIOD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")
PERIOD_IN_TEXT_RE = re.compile(r"\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}")


def _empty(value):
    return value is None or (isinstance(value, str) and not value.strip())


def parse_date(value):
    """`YYYY-MM-DD` -> date, anything else -> None."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _exact_period(value):
    match = PERIOD_RE.match(value) if isinstance(value, str) else None
    if not match:
        return False
    start, end = parse_date(match.group(1)), parse_date(match.group(2))
    return bool(start and end and start <= end)


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _section(text, heading):
    match = re.search(rf"^## {re.escape(heading)}[ \t]*$", text, re.MULTILINE)
    if not match:
        return None
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


class _Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)


def _has_question(questions, metric_id, field, key="metric_id"):
    """`key` is "value_item_id" for a question about a value item."""
    for q in questions:
        if not isinstance(q, dict) or q.get(key) != metric_id:
            continue
        asked = q.get("field")
        if isinstance(asked, str) and (field == asked or field.startswith(asked + ".")
                                       or asked.startswith(field + ".")):
            return True
    return False


def _has_general_question(questions, field):
    """An OpenQuestion with neither metric_id nor value_item_id covers a
    top-level field such as `at_scale.volume`; one asked about "at_scale"
    covers all of them."""
    for q in questions:
        if not isinstance(q, dict) or q.get("metric_id") is not None \
                or q.get("value_item_id") is not None:
            continue
        asked = q.get("field")
        if isinstance(asked, str) and (field == asked or field.startswith(asked + ".")
                                       or asked.startswith(field + ".")):
            return True
    return False


def _check_baseline(m, label, report):
    baseline = m.get("baseline")
    if baseline is None:
        return ["baseline"]
    if not isinstance(baseline, dict):
        report.error(f"{label}: baseline має бути об'єктом {{value, period, source, kind}} або null")
        return []
    not_applicable = baseline.get("not_applicable_why")
    if _empty(baseline.get("value")):
        # v1.1: «неможливо зміряти» is answered in the JSON itself.
        return ["baseline"] if _empty(not_applicable) else []
    if not _empty(not_applicable):
        report.error(f"{label}: у baseline є і значення, і not_applicable_why. Або число з "
                     f"джерелом, або причина, чому зміряти неможливо")
        return []
    if _empty(baseline.get("source")):
        report.error(f"{label}: у baseline є число, але не вказано джерело (source)")
    if baseline.get("kind") not in KINDS:
        report.error(f"{label}: baseline.kind має бути measured або estimate, "
                     f"зараз {baseline.get('kind')!r}")
    if _empty(baseline.get("period")):
        report.error(f"{label}: у baseline не вказано період (period)")
    elif baseline.get("kind") == "measured" and not _exact_period(baseline.get("period")):
        report.error(f"{label}: baseline позначено як measured, але період "
                     f"{baseline.get('period')!r} не у форматі YYYY-MM-DD..YYYY-MM-DD. "
                     f"Без точних дат це оцінка (estimate)")
    return []


def _check_target(m, label, report):
    target = m.get("target")
    if target is None:
        return ["target"]
    if not isinstance(target, dict):
        report.error(f"{label}: target має бути об'єктом {{value, why}} або null")
        return []
    if _empty(target.get("value")):
        return ["target"]
    if _empty(target.get("why")):
        return ["target.why"]
    return []


def _check_money(m, label, report):
    money = m.get("money")
    if money is None:
        return []
    if not isinstance(money, dict):
        report.error(f"{label}: money має бути об'єктом {{rate_usd_per_unit, realization, "
                     f"realization_why}} або null")
        return []
    missing = []
    for key in ("rate_usd_per_unit", "realization"):
        value = money.get(key)
        if value is None:
            missing.append(f"money.{key}")
        elif not is_number(value):
            report.error(f"{label}: money.{key} має бути числом, зараз {value!r}")
    realization = money.get("realization")
    if is_number(realization) and not 0 <= realization <= 1:
        report.error(f"{label}: money.realization має бути від 0 до 1, зараз {realization}")
    if m.get("type") == "time" and _empty(money.get("realization_why")):
        missing.append("money.realization_why")
    return missing


def _check_measurements(m, label, report):
    measurements = m.get("measurements")
    if not isinstance(measurements, list):
        report.error(f"{label}: measurements має бути списком (може бути порожнім [])")
        return
    for i, entry in enumerate(measurements, 1):
        where = f"{label}, замір №{i}"
        if not isinstance(entry, dict):
            report.error(f"{where}: має бути об'єктом")
            continue
        absent = [k for k in MEASUREMENT_KEYS if k not in entry]
        if absent:
            report.error(f"{where}: бракує полів {', '.join(absent)}")
        if parse_date(entry.get("date")) is None:
            report.error(f"{where}: date має бути датою YYYY-MM-DD, зараз {entry.get('date')!r}")
        if entry.get("kind") not in KINDS:
            report.error(f"{where}: kind має бути measured або estimate, зараз {entry.get('kind')!r}")
        elif entry.get("kind") == "measured" and not _exact_period(entry.get("period")):
            report.error(f"{where}: позначено як measured, але період {entry.get('period')!r} "
                         f"не у форматі YYYY-MM-DD..YYYY-MM-DD. Без точних дат це оцінка (estimate)")
        if _empty(entry.get("value")):
            report.error(f"{where}: немає значення (value)")
        elif _empty(entry.get("source")):
            report.error(f"{where}: є число, але не вказано джерело (source)")


def _check_metric(m, index, report):
    if not isinstance(m, dict):
        report.error(f"Метрика №{index}: має бути об'єктом")
        return None, None, []
    label = f"Метрика «{m.get('name') or m.get('id') or index}»"
    absent = [k for k in METRIC_KEYS if k not in m]
    if absent:
        report.error(f"{label}: бракує обов'язкових полів {', '.join(absent)} "
                     f"(порожнє значення пишеться як null, а не пропускається)")
    metric_id = m.get("id")
    if not isinstance(metric_id, str) or not KEBAB_RE.match(metric_id):
        report.error(f"{label}: id має бути у kebab-case (малі латинські літери, цифри, дефіс), "
                     f"зараз {metric_id!r}")
    if _empty(m.get("name")):
        report.error(f"{label}: немає назви (name)")
    if m.get("type") not in TYPES:
        report.error(f"{label}: type має бути одним із {', '.join(sorted(TYPES))}, "
                     f"зараз {m.get('type')!r}")
    if _empty(m.get("unit")):
        report.error(f"{label}: немає одиниці (unit)")
    if m.get("direction") not in DIRECTIONS:
        report.error(f"{label}: direction має бути down (менше краще) або up, "
                     f"зараз {m.get('direction')!r}")
    if not isinstance(m.get("primary"), bool):
        report.error(f"{label}: primary має бути true або false")

    missing = _check_baseline(m, label, report) + _check_target(m, label, report)
    if _empty(m.get("method")):
        missing.append("method")
    every = m.get("check_every_days")
    if every is None:
        missing.append("check_every_days")
    elif not isinstance(every, int) or isinstance(every, bool) or every <= 0:
        report.error(f"{label}: check_every_days має бути цілим числом днів більше 0, зараз {every!r}")
    missing += _check_money(m, label, report)
    _check_measurements(m, label, report)
    return metric_id, m.get("name"), missing


def _check_value_item(item, index, metric_ids, report):
    """Returns (id, missing fields an OpenQuestion may stand in for)."""
    if not isinstance(item, dict):
        report.error(f"Стаття цінності №{index}: має бути об'єктом")
        return None, []
    label = f"Стаття цінності «{item.get('id') or item.get('title') or index}»"
    absent = [k for k in VALUE_ITEM_KEYS if k not in item]
    if absent:
        report.error(f"{label}: бракує обов'язкових полів {', '.join(absent)} "
                     f"(порожнє значення пишеться як null, а не пропускається)")

    item_id = item.get("id")
    if not isinstance(item_id, str) or not KEBAB_RE.match(item_id):
        report.error(f"{label}: id має бути у kebab-case (малі латинські літери, цифри, дефіс), "
                     f"зараз {item_id!r}")
    category = item.get("category")
    if category not in CATEGORIES:
        report.error(f"{label}: category має бути однією з {', '.join(CATEGORIES)}, "
                     f"зараз {category!r}")
    if _empty(item.get("title")):
        report.error(f"{label}: немає опису, що саме дає стаття (title)")

    missing = []
    usd = item.get("usd_per_month")
    if usd is None:
        missing.append("usd_per_month")
    elif not is_number(usd):
        report.error(f"{label}: usd_per_month має бути числом, зараз {usd!r}")
    elif category == "run_cost" and usd > 0:
        report.error(f"{label}: експлуатація (run_cost) це витрата, usd_per_month має бути "
                     f"від'ємним або 0, зараз {usd}")
    elif category in CATEGORIES and category != "run_cost" and usd <= 0:
        report.error(f"{label}: для категорії {category} usd_per_month має бути більше 0, "
                     f"зараз {usd}. Витрати на саму автоматизацію пишуться окремою статтею run_cost")

    kind = item.get("kind")
    if kind in ("confirmed", "verified", "підтверджено"):
        report.error(f"{label}: kind {kind!r} не можна ставити в репо. «Підтверджено» ставить "
                     f"тільки дашборд після вердикту адміністратора; тут measured або estimate")
    elif kind not in KINDS:
        report.error(f"{label}: kind має бути measured або estimate, зараз {kind!r}")
    basis = item.get("basis")
    if basis not in BASES:
        report.error(f"{label}: basis має бути data (обсяг з даних) або assumption (припущення), "
                     f"зараз {basis!r}")
    if kind == "measured":
        text = " ".join(str(item.get(k) or "") for k in ("source", "formula"))
        if basis != "data":
            report.error(f"{label}: measured можливий лише коли обсяг узято з даних (basis: data)")
        if not any(_exact_period(p) for p in PERIOD_IN_TEXT_RE.findall(text)):
            report.error(f"{label}: позначено як measured, але в source чи formula немає точного "
                         f"періоду заміру YYYY-MM-DD..YYYY-MM-DD. Без нього це оцінка (estimate)")

    for key in ("formula", "why", "source"):
        if _empty(item.get(key)):
            missing.append(key)
    if "as_of" in item and parse_date(item.get("as_of")) is None:
        report.error(f"{label}: as_of має бути датою YYYY-MM-DD, зараз {item.get('as_of')!r}")
    linked = item.get("metric_ids")
    if linked is not None:
        if not isinstance(linked, list) or not all(isinstance(x, str) for x in linked):
            report.error(f"{label}: metric_ids має бути списком id метрик або null")
        else:
            for metric_id in linked:
                if metric_id not in metric_ids:
                    report.warn(f"{label}: metric_ids посилається на метрику {metric_id!r}, "
                                f"якої немає в metrics")
    return item_id, missing


def _check_value_items(items, metrics, metric_ids, report):
    """v1.1 `value_items`. Returns [(id, missing fields)] for the OpenQuestion check."""
    if not isinstance(items, list):
        report.error("value_items має бути списком або відсутнім")
        return []
    checked = [_check_value_item(x, i, metric_ids, report) for i, x in enumerate(items, 1)]
    ids = [c[0] for c in checked if isinstance(c[0], str)]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        report.error(f"Стаття цінності: id {dup!r} повторюється. Кожна стаття має свій унікальний id")
    if items:
        for m in metrics:
            if isinstance(m, dict) and m.get("money") is not None:
                report.error(f"Метрика «{m.get('name') or m.get('id')}»: є money, а гроші вже "
                             f"розкладені у value_items. Гроші рахуємо в одному місці: коли є "
                             f"value_items, money у метриках має бути null")
    return checked


def _check_at_scale(at_scale, value_items, report):
    """v1.1 `at_scale`: the value at the agreed target volume for an automation
    that runs on a small one today. Returns the missing askable fields, already
    prefixed with "at_scale." (an OpenQuestion with both ids null stands in for
    them)."""
    if at_scale is None:
        return []
    if not isinstance(at_scale, dict):
        report.error("at_scale має бути об'єктом {usd_per_month_low, usd_per_month_high, volume, "
                     "condition, formula, source, as_of} або null")
        return []
    if not (isinstance(value_items, list) and value_items):
        report.error("at_scale є, а статей цінності немає: спочатку статті цінності на поточному "
                     "обсязі. Повний масштаб це та сама формула на обсязі ТЗ, без них його нема з "
                     "чого рахувати")
    absent = [k for k in AT_SCALE_REQUIRED if k not in at_scale]
    if absent:
        report.error(f"Повний масштаб: бракує обов'язкових полів {', '.join(absent)} "
                     f"(порожнє значення пишеться як null, а не пропускається). Необов'язкове лише "
                     f"usd_per_month_high: одна цифра замість вилки")

    missing = []
    low = at_scale.get("usd_per_month_low")
    if _empty(low):
        missing.append("usd_per_month_low")
    elif not is_number(low):
        report.error(f"Повний масштаб: usd_per_month_low має бути числом, зараз {low!r}")
    elif low <= 0:
        report.error(f"Повний масштаб: usd_per_month_low має бути більше 0, зараз {low}. Це чисте "
                     f"значення на місяць після експлуатації, а не оборот")
    high = at_scale.get("usd_per_month_high")
    if not _empty(high):
        if not is_number(high):
            report.error(f"Повний масштаб: usd_per_month_high має бути числом або null "
                         f"(одна цифра замість вилки), зараз {high!r}")
        elif is_number(low) and high < low:
            report.error(f"Повний масштаб: usd_per_month_high ({high}) менше за usd_per_month_low "
                         f"({low}). Верхня межа вилки не може бути нижчою за нижню")
    for key in ("volume", "condition", "formula", "source"):
        if _empty(at_scale.get(key)):
            missing.append(key)
    as_of = at_scale.get("as_of")
    if _empty(as_of):
        missing.append("as_of")
    elif parse_date(as_of) is None:
        report.error(f"Повний масштаб: as_of має бути датою YYYY-MM-DD, зараз {as_of!r}")
    if "kind" in at_scale:
        report.warn("Повний масштаб: ключ kind тут не потрібен, at_scale завжди оцінка "
                    "(обсягу ТЗ ще немає, тому зміряти його ніде). Видали ключ")
    return [f"at_scale.{field}" for field in missing]


def _usd(value):
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(round(value)):,}".replace(",", " ")


def value_summary(data):
    """The two lines dashboard/ROI.md's "## Actual Results" starts with when
    there are value_items, or None - same as the skill's value_summary().
    `at_scale` is deliberately not part of either line: it's what the
    automation would be worth at a volume it doesn't run on yet."""
    items = [x for x in (data.get("value_items") or []) if isinstance(x, dict)
             and is_number(x.get("usd_per_month"))] if isinstance(data, dict) else []
    if not items:
        return None
    measured = sum(x["usd_per_month"] for x in items if x.get("kind") == "measured")
    estimate = sum(x["usd_per_month"] for x in items if x.get("kind") != "measured")
    per_category = []
    for code, name in CATEGORIES.items():
        values = [x["usd_per_month"] for x in items if x.get("category") == code]
        if values:
            per_category.append(f"{name} {_usd(sum(values))}")
    return (f"Заміряно {_usd(measured)}/міс, оцінка {_usd(estimate)}/міс",
            "За категоріями: " + ", ".join(per_category))


def _check_open_questions(questions, metric_ids, report, value_item_ids=frozenset()):
    if not isinstance(questions, list):
        report.error("open_questions має бути списком (може бути порожнім [])")
        return []
    for i, q in enumerate(questions, 1):
        where = f"Відкрите питання №{i}"
        if not isinstance(q, dict):
            report.error(f"{where}: має бути об'єктом")
            continue
        absent = [k for k in OPEN_QUESTION_KEYS if k not in q]
        if absent:
            report.error(f"{where}: бракує полів {', '.join(absent)}")
        for key in ("field", "question", "ask_whom"):
            if key in q and _empty(q.get(key)):
                report.error(f"{where}: порожнє поле {key}")
        if "since" in q and parse_date(q.get("since")) is None:
            report.error(f"{where}: since має бути датою YYYY-MM-DD, зараз {q.get('since')!r}")
        metric_id = q.get("metric_id")
        item_id = q.get("value_item_id")
        if metric_id is not None and item_id is not None:
            report.error(f"{where}: вказано і metric_id, і value_item_id. Питання стосується рівно "
                         f"одного з них (або обидва null, якщо питання загальне)")
        if metric_id is not None and metric_id not in metric_ids:
            report.warn(f"{where}: посилається на метрику {metric_id!r}, якої немає в metrics")
        if item_id is not None and item_id not in value_item_ids:
            report.warn(f"{where}: посилається на статтю цінності {item_id!r}, якої немає в value_items")
    return questions


def overdue_date(launched_at, check_every_days, measurement_dates):
    """When the next measurement is due (launched_at or the latest measurement,
    whichever is later, plus check_every_days), or None if either input is
    missing. Shared with the status calculation in metrics_sync."""
    if launched_at is None or not isinstance(check_every_days, int) or check_every_days <= 0:
        return None
    last = max([launched_at] + [d for d in measurement_dates if d])
    return last + datetime.timedelta(days=check_every_days)


def has_measured_after_launch(data):
    launched = parse_date(data.get("launched_at")) if isinstance(data, dict) else None
    if launched is None:
        return False
    for m in data.get("metrics") or []:
        if not isinstance(m, dict):
            continue
        for x in m.get("measurements") or []:
            if isinstance(x, dict) and x.get("kind") == "measured":
                date = parse_date(x.get("date"))
                if date and date > launched:
                    return True
    return False


def validate(data, today=None, roi_md=None):
    """Returns (errors, warnings) for parsed metrics.json `data`. `roi_md` is
    dashboard/ROI.md's text when the same sync fetched it, for the
    "Measured needs a measured measurement" cross-check."""
    report = _Report()
    today = today or datetime.date.today()
    if not isinstance(data, dict):
        report.error("Корінь metrics.json має бути об'єктом {version, launched_at, metrics, open_questions}")
        return report.errors, report.warnings
    for key in ("version", "launched_at", "metrics", "open_questions"):
        if key not in data:
            report.error(f"Бракує поля верхнього рівня {key}")
    if "_приклад" in data:
        report.error("У metrics.json лишився ключ _приклад із шаблону. Це приклад, не дані: видали його")
    # v1.1 is additive, so it keeps version 1.
    if "version" in data and data.get("version") != 1:
        report.error(f"version має бути 1, зараз {data.get('version')!r}")
    launched = None
    if data.get("launched_at") is not None:
        launched = parse_date(data.get("launched_at"))
        if launched is None:
            report.error(f"launched_at має бути датою YYYY-MM-DD або null, зараз {data.get('launched_at')!r}")

    metrics = data.get("metrics")
    if not isinstance(metrics, list):
        if "metrics" in data:
            report.error("metrics має бути списком")
        metrics = []
    if not metrics:
        report.error("У metrics немає жодної метрики. Порожній список означає, що метрики не зібрані: "
                     "спитай автоматизатора, що міряємо")

    checked = [_check_metric(m, i, report) for i, m in enumerate(metrics, 1)]
    ids = [c[0] for c in checked if isinstance(c[0], str)]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        report.error(f"id {dup!r} повторюється. Кожна метрика має свій унікальний id")
    primaries = sum(1 for m in metrics if isinstance(m, dict) and m.get("primary") is True)
    if metrics and primaries != 1:
        report.error(f"Головна метрика (primary: true) має бути рівно одна, зараз {primaries}")

    value_items = data.get("value_items")
    checked_items = []
    if value_items is not None:
        checked_items = _check_value_items(value_items, metrics, set(ids), report)
    item_ids = {c[0] for c in checked_items if isinstance(c[0], str)}

    questions = _check_open_questions(data.get("open_questions", []), set(ids), report, item_ids)

    for field in _check_at_scale(data.get("at_scale"), value_items, report):
        if _has_general_question(questions, field):
            continue
        report.error(f"Повний масштаб: поле {field} порожнє і немає відкритого питання "
                     f"(open_questions) з metric_id=null, value_item_id=null, field={field!r}. "
                     f"Спитай автоматизатора: значення з джерелом або «не знаю, знає: ...»")

    for metric_id, name, missing in checked:
        if metric_id is None:
            continue
        for field in missing:
            if not _has_question(questions, metric_id, field):
                report.error(f"Метрика «{name or metric_id}»: поле {field} порожнє і немає відкритого "
                             f"питання (open_questions) з metric_id={metric_id!r}, field={field!r}. "
                             f"Спитай автоматизатора: значення з джерелом, «не знаю, знає: ...» "
                             f"або «неможливо зміряти, бо ...» (для baseline причина йде в "
                             f"baseline.not_applicable_why)")
    for item_id, missing in checked_items:
        for field in missing:
            if item_id is None or _has_question(questions, item_id, field, key="value_item_id"):
                continue
            report.error(f"Стаття цінності «{item_id}»: поле {field} порожнє і немає відкритого "
                         f"питання з value_item_id={item_id!r}, field={field!r}. Спитай "
                         f"автоматизатора: значення з джерелом або «не знаю, знає: ...»")

    for m in metrics:
        if not isinstance(m, dict):
            continue
        dates = [parse_date(x.get("date")) for x in m.get("measurements") or [] if isinstance(x, dict)]
        due = overdue_date(launched, m.get("check_every_days"), dates)
        if due and due < today:
            report.warn(f"Метрика «{m.get('name') or m.get('id')}»: замір прострочений, мав бути "
                        f"до {due.isoformat()}")

    if roi_md is not None:
        confidence = _section(roi_md, "Confidence")
        first = confidence.strip().split()[0] if confidence and confidence.strip() else ""
        if first.lower().startswith("measured") and not has_measured_after_launch(data):
            report.error("dashboard/ROI.md каже Measured, але в metrics.json немає жодного заміру "
                         "з kind: measured після launched_at. Має бути Estimated")
        summary = value_summary(data)
        if summary:
            results = _section(roi_md, "Actual Results") or ""
            lines = [ln.strip() for ln in re.sub(r"<!--.*?-->", "", results, flags=re.DOTALL)
                     .splitlines() if ln.strip()]
            if lines[:2] != list(summary):
                report.error("dashboard/ROI.md «## Actual Results» має починатися з двох рядків "
                             f"підсумку цінності з value_items: «{summary[0]}» і «{summary[1]}». "
                             "Перегенеруй ROI.md")
    return report.errors, report.warnings


def parse_and_validate(text, today=None, roi_md=None):
    """Returns (data or None, errors, warnings). Invalid JSON is one error and
    no data."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        where = f", рядок {exc.lineno}, позиція {exc.colno}" if isinstance(exc, json.JSONDecodeError) else ""
        msg = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        return None, [f"metrics.json не є валідним JSON: {msg}{where}"], []
    errors, warnings = validate(data, today=today, roi_md=roi_md)
    return data, errors, warnings
