"""Validation of an automation repo's dashboard/metrics.json against the v1
contract (docs/metrics-contract.md). The rules and their messages mirror
stage-1-supplax's scripts/check_metrics.py (the reference validator the
automators run before pushing) so a file that passes there passes here and
the other way round - but this module doesn't import or depend on the skill.

One rule from the reference is skipped: matching metric names against
docs/roi_explained.md's "## Метрики", since the sync never fetches that file.
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
KEBAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PERIOD_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")


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


def _has_question(questions, metric_id, field):
    for q in questions:
        if not isinstance(q, dict) or q.get("metric_id") != metric_id:
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
    if _empty(baseline.get("value")):
        return ["baseline"]
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


def _check_open_questions(questions, metric_ids, report):
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
        if metric_id is not None and metric_id not in metric_ids:
            report.warn(f"{where}: посилається на метрику {metric_id!r}, якої немає в metrics")
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

    questions = _check_open_questions(data.get("open_questions", []), set(ids), report)
    for metric_id, name, missing in checked:
        if metric_id is None:
            continue
        for field in missing:
            if not _has_question(questions, metric_id, field):
                report.error(f"Метрика «{name or metric_id}»: поле {field} порожнє і немає відкритого "
                             f"питання (open_questions) з metric_id={metric_id!r}, field={field!r}. "
                             f"Спитай автоматизатора: значення з джерелом, «не знаю, знає: ...» "
                             f"або «неможливо зміряти, бо ...» (тоді причина в Caveats)")

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
