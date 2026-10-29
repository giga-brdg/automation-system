"""Aggregation for the portfolio-wide /value page (value_dashboard_page in
src/app.py) - turns a list of Automation rows into the numbers the template
needs. Kept separate from the route so the arithmetic is unit-testable
without a request/response round trip.

Two kinds of automation share the page. One that has synced a valid
dashboard/metrics.json is read through src/metrics_sync.py's
automation_metrics()/compute_status - the single source of every metric
status, result and money figure. One that never has ("старий формат") keeps
its ROIEntry numbers, but "Виміряно" there now means a real
measured_hours_per_month number, never the free-text confidence label (the
mismatch docs in metrics-research/01_current_value_page.md section 3.9).

Every total sums one honest unit (hours/month, dollars/month) and a missing
number stays None - shown as "немає даних" or a dash, never as 0.
dev_hours stays out of every monthly total: it's a one-time cost.

The second half of the module is text: the page is read by people who don't
know the contract's field names or the repos' script paths, so dates,
periods, sources, open questions, journal entries and validation errors are
turned into plain Ukrainian here rather than in the template.
"""
import datetime
import decimal
import math
import re

from . import metrics_sync
from .metrics_contract import CATEGORIES, is_number, parse_date
from .models import (
    AutomationEvent,
    AutomationMetric,
    AutomationValueItem,
    MetricsSnapshot,
)

METRIC_LABELS = {
    "time_saved": "Час ручної праці",
    "conversion": "Конверсія",
    "quality": "Якість",
    "cost": "Витрати",
}
METRIC_PILL_CLASS = {
    "time_saved": "metric-pill-time",
    "conversion": "metric-pill-conv",
    "quality": "metric-pill-qual",
    "cost": "metric-pill-cost",
}
METRIC_ORDER = ("time_saved", "conversion", "quality", "cost")

# Contract `type` -> label/bar colour/pill. The first four reuse Stage 1's
# palette (supplax-overrides.css --metric-*); revenue/other have no slot of
# their own there, so they borrow Bootstrap's own variables instead of new CSS.
TYPE_LABELS = {
    "time": "Час ручної праці",
    "conversion": "Конверсія",
    "quality": "Якість",
    "cost": "Витрати",
    "revenue": "Дохід",
    "other": "Інше",
}
TYPE_ORDER = ("time", "conversion", "quality", "cost", "revenue", "other")
TYPE_CSS_VAR = {
    "time": "--metric-time", "conversion": "--metric-conv", "quality": "--metric-qual",
    "cost": "--metric-cost", "revenue": "--bs-purple", "other": "--bs-secondary",
}
TYPE_PILL_CLASS = {
    "time": "metric-pill-time", "conversion": "metric-pill-conv",
    "quality": "metric-pill-qual", "cost": "metric-pill-cost",
}
STAGE1_TO_TYPE = {"time_saved": "time", "conversion": "conversion", "quality": "quality", "cost": "cost"}

# Units a time metric's value can be summed in for the big hours/month card
# and the net-balance chart.
HOURS_PER_MONTH_UNITS = {"год/міс", "год/місяць", "годин/міс", "годин на місяць"}

TILE_ORDER = ("confirmed", "measured", "awaiting", "no_data")

ROI_FIELD_LABELS = {
    "measured_hours_per_month": "виміряно годин/міс",
    "confidence": "мітка «виміряно/оцінка»",
    "dev_hours": "години на розробку",
    "maintenance_hours_per_month": "підтримка, год/міс",
    "baseline_cycle_minutes": "хвилин на цикл до",
    "baseline_frequency_per_month": "разів на місяць до",
    "target_cycle_minutes": "хвилин на цикл після",
    "target_frequency_per_month": "разів на місяць після",
}
# Contract field names as they show up in open questions and errors.
CONTRACT_FIELD_LABELS = {
    "baseline": "До",
    "target": "Ціль",
    "target.why": "Чому така ціль",
    "method": "Як перевіряємо",
    "check_every_days": "Як часто міряти",
    "money": "Гроші",
    "money.realization_why": "Чому такий коефіцієнт",
    "money.rate_usd_per_unit": "Ставка",
    "money.realization": "Коефіцієнт реалізації",
    "usd_per_month": "$/міс",
    "formula": "Як пораховано",
    "why": "Чому так",
    "source": "Звідки дані",
    "at_scale": "Повний масштаб",
    "at_scale.usd_per_month_low": "Сума на повному масштабі",
    "at_scale.usd_per_month_high": "Верхня межа на повному масштабі",
    "at_scale.volume": "Обсяг",
    "at_scale.condition": "Умова",
    "at_scale.formula": "Як пораховано повний масштаб",
    "at_scale.source": "Звідки дані про повний масштаб",
    "at_scale.as_of": "Станом на",
}
# Value-item category -> (pill kind, CSS class). Time and cost savings reuse
# the metric pills; the rest have no pill colour of their own, so they use
# Bootstrap's light badges rather than new CSS.
CATEGORY_PILL = {
    "time": ("pill", "metric-pill-time"),
    "cost_savings": ("pill", "metric-pill-cost"),
    "revenue": ("badge", "bg-light-success"),
    "losses": ("badge", "bg-light-warning"),
    "run_cost": ("badge", "bg-light-danger"),
}
BASIS_LABELS = {"data": "дані", "assumption": "припущення"}
VALUE_STATUS_BADGE = {"confirmed": "bg-light-success", "partial": "bg-light-success",
                      "measured": "bg-light-primary", "estimate": "bg-light-warning",
                      "rejected": "bg-light-danger"}
KIND_LABELS = {"measured": "замір", "estimate": "оцінка"}
# v1.1 at_scale: the fields a general open question can stand in for, and the
# one label that says why its money is shown but never added to anything.
AT_SCALE_FIELDS = ("usd_per_month_low", "volume", "condition", "formula", "source", "as_of")
AT_SCALE_NOTE = "оцінка, у суми не входить"
ASK_WHOM_LABELS = {
    "automator": "автоматизатор",
    "department_lead": "керівник відділу",
    "crm_query": "запит у CRM",
    "manual_measurement": "ручний замір",
}
SYNC_AUTHOR = "GitHub"
REJECTED_HINT = "Автоматизатор: запустити automation-portfolio-sync у репо, він спитає відсутнє."


def target_metrics(automation):
    """Token list from Stage 1 Phase 1's target_metric_type answer (see
    src/stage1_questions.py) - [] for an automation that hasn't answered
    Stage 1 yet, or whose stage1_answers predates this field."""
    phase1 = (automation.stage1_answers or {}).get("1", {})
    tokens = phase1.get("target_metric_type") or []
    return [t for t in tokens if t in METRIC_LABELS]


# --- Text formatting (pure, also exposed to the template) --------------------

def fmt_number(value):
    """12.8 -> "12,8", 61.0 -> "61": a decimal comma, at most two places."""
    if value is None:
        return "немає даних"
    # ROIEntry's Numeric columns come back as Decimal("8.00").
    if is_number(value) or isinstance(value, decimal.Decimal):
        value = float(value)
        return f"{value:.0f}" if value == int(value) else f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return str(value)


def fmt_hours(value, signed=False):
    """Hours with one decimal and a decimal comma: 5 -> "5,0", signed -> "+5,0"."""
    text = f"{value:+.1f}" if signed else f"{value:.1f}"
    return text.replace(".", ",").replace("-", MINUS)


MINUS = "−"
# Thousands separator: a no-break space, so "$1 220" never splits across lines
# in the narrow value column.
THOUSANDS_SEP = " "


def fmt_usd(value):
    """The one money format on /value (tiles, value column, totals, journal):
    whole dollars, no cents, a no-break space between thousands and a real
    minus sign in front - 575 -> "$575", 1220 -> "$1 220", -40 -> "−$40".
    None -> "—"."""
    if value is None:
        return "—"
    dollars = round(float(value))
    sign = MINUS if dollars < 0 else ""
    return f"{sign}${abs(dollars):,}".replace(",", THOUSANDS_SEP)


def fmt_date(value):
    """date/datetime or "YYYY-MM-DD" -> "DD.MM.YYYY"; anything else as is."""
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime("%d.%m.%Y")
    parsed = parse_date(value) if isinstance(value, str) else None
    return parsed.strftime("%d.%m.%Y") if parsed else (value or "")


_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def fmt_period(value):
    """"2026-05-20..2026-07-21" -> "20.05–21.07.2026" (year once when both
    ends share it). A text period, or anything unparseable, comes back as is
    with any ISO dates inside it reformatted."""
    if not isinstance(value, str):
        return ""
    start, sep, end = value.partition("..")
    a, b = parse_date(start), parse_date(end) if sep else None
    if a and b:
        if a.year == b.year:
            return f"{a.strftime('%d.%m')}–{b.strftime('%d.%m.%Y')}"
        return f"{a.strftime('%d.%m.%Y')}–{b.strftime('%d.%m.%Y')}"
    return _ISO_DATE_RE.sub(lambda m: f"{m.group(3)}.{m.group(2)}.{m.group(1)}", value)


# A path with a slash, a file with a code extension, or a _private_name /
# snake_case identifier: real, but meaningless to someone reading /value.
_TECH_TOKEN_RE = re.compile(r"`[^`]*`|[\w.\-]*/[\w./\-]+|\b\w+\.(?:py|js|ts|sh|sql|jsonl?|md|csv|ya?ml|txt|log)\b|\b_\w+\b|\b[a-z]+_[a-z_]+\b")


def split_source(value):
    """(plain text, technical part) of a source string. The plain part is
    what the row shows; the technical part (script paths, identifiers) goes
    to a title and small grey text. Never drops information: the full
    original is still the title."""
    if not value:
        return "не вказано", ""
    tech = [m.group(0).strip("`") for m in _TECH_TOKEN_RE.finditer(value)]
    plain = _TECH_TOKEN_RE.sub("", value)
    plain = re.sub(r"\b(за|у|в|з|через)\s*(?=[),;:]|$)", "", plain)
    plain = re.sub(r"\(\s*[,;]?\s*\)", "", plain)
    plain = re.sub(r"\s+([,;:)])", r"\1", plain)
    plain = re.sub(r"\(\s+", "(", plain)
    plain = re.sub(r"([,;])\s*(?=[,;)])", "", plain)
    plain = re.sub(r"\s{2,}", " ", plain).strip(" ,;:")
    plain = _ISO_DATE_RE.sub(lambda m: f"{m.group(3)}.{m.group(2)}.{m.group(1)}", plain)
    return (plain or "дані скрипта в репозиторії"), ", ".join(dict.fromkeys(tech))


def field_label(field):
    return CONTRACT_FIELD_LABELS.get(field, field)


def ask_whom_label(value):
    return ASK_WHOM_LABELS.get(value, value)


def _value_with_unit(value, unit):
    text = fmt_number(value)
    if not is_number(value) or not unit:
        return text
    return f"{text}%" if unit.strip().startswith("%") else f"{text} {unit}"


_MISSING_FIELD_RE = re.compile(r"^Метрика «(.+?)»: поле (\S+) порожнє і немає відкритого питання")
_MISSING_ITEM_FIELD_RE = re.compile(r"^Стаття цінності «(.+?)»: поле (\S+) порожнє і немає відкритого питання")
_MONEY_TWICE_RE = re.compile(r"^Метрика «(.+?)»: є money, а гроші вже розкладені у value_items")
_PRIMARY_RE = re.compile(r"^Головна метрика \(primary: true\) має бути рівно одна, зараз (\d+)")
_NO_METRICS_RE = re.compile(r"^У metrics немає жодної метрики")
_JSON_RE = re.compile(r"не є валідним JSON")
_MEASURED_ROI_RE = re.compile(r"^dashboard/ROI\.md каже Measured")


def humanize_error(message):
    """A contract-validation error as one plain sentence. The common cases
    get their own wording; anything else keeps the validator's own Ukrainian
    message, which is already readable, minus quoted field names."""
    m = _MISSING_FIELD_RE.match(message)
    if m:
        return (f"«{m.group(1)}»: не заповнено «{field_label(m.group(2))}», "
                f"і не записано, кого про це спитати.")
    m = _MISSING_ITEM_FIELD_RE.match(message)
    if m:
        return (f"Стаття цінності «{m.group(1)}»: не заповнено «{field_label(m.group(2))}», "
                f"і не записано, кого про це спитати.")
    m = _MONEY_TWICE_RE.match(message)
    if m:
        return (f"«{m.group(1)}»: гроші пораховані і в метриці, і в статтях цінності. "
                f"Мають бути в одному місці.")
    m = _PRIMARY_RE.match(message)
    if m:
        count = int(m.group(1))
        return ("Не вибрано головну метрику." if count == 0
                else f"Головних метрик {count}, а має бути одна.")
    if _NO_METRICS_RE.match(message):
        return "У файлі немає жодної метрики."
    if _JSON_RE.search(message):
        return "Файл пошкоджений: його не вдалося прочитати."
    if _MEASURED_ROI_RE.match(message):
        return "ROI.md каже «виміряно», але жодного заміру після запуску у файлі немає."
    return message


def _fmt_baseline_parts(b):
    """{"value", "period", "kind", "source", "source_tech"} for display,
    {"not_applicable": reason} for a baseline that can't be measured, or None."""
    if isinstance(b, dict) and b.get("value") is None and (b.get("not_applicable_why") or "").strip():
        return {"not_applicable": b["not_applicable_why"].strip()}
    if not isinstance(b, dict) or b.get("value") is None:
        return None
    source, tech = split_source(b.get("source"))
    return {"value": b.get("value"), "period": fmt_period(b.get("period")),
            "kind": KIND_LABELS.get(b.get("kind"), b.get("kind") or ""),
            "source": source, "source_tech": tech, "source_full": b.get("source") or ""}


def _short_value(event_type, value, unit):
    if value is None:
        return "немає"
    if isinstance(value, dict) and "value" in value:
        return _value_with_unit(value.get("value"), unit)
    return fmt_number(value)


def _item_change_text(old, new):
    parts = []
    if "usd_per_month" in (new or {}):
        parts.append(f"{fmt_usd(old.get('usd_per_month'))} → {fmt_usd(new.get('usd_per_month'))}/міс")
    if "formula" in (new or {}):
        parts.append(f"формула: «{old.get('formula') or 'немає'}» → «{new.get('formula') or 'немає'}»")
    if "kind" in (new or {}):
        parts.append(f"{KIND_LABELS.get(old.get('kind'), old.get('kind'))} → "
                     f"{KIND_LABELS.get(new.get('kind'), new.get('kind'))}")
    others = [f for f in (new or {}) if f not in ("usd_per_month", "formula", "kind", "title")]
    if others:
        parts.append("також " + ", ".join(field_label(f) for f in others))
    return "; ".join(parts) or "опис"


def describe_event(event, metrics, items=None):
    """One journal row as plain Ukrainian {when, who, text}. `metrics` maps
    metric_key -> AutomationMetric, for names and units; `items` maps a
    value item's key -> AutomationValueItem (value_* events keep the item's
    key in metric_key)."""
    items = items or {}
    who = SYNC_AUTHOR if event.actor_label == metrics_sync.SYNC_ACTOR else event.actor_label
    if event.event_type.startswith("value_"):
        old, new = event.old_value or {}, event.new_value or {}
        row = items.get(event.metric_key)
        title = (row.title if row else None) or new.get("title") or old.get("title") or event.metric_key
        t = event.event_type
        if t == "value_item_added":
            amount = new.get("usd_per_month")
            text = (f"Додано статтю цінності: «{title}», "
                    + (f"{fmt_usd(amount)}/міс" if amount is not None else "сума ще не задана"))
        elif t == "value_item_changed":
            text = f"Змінено статтю «{title}»: {_item_change_text(old, new)}"
        elif t == "value_item_removed":
            text = f"Статтю прибрано з репо: «{title}»"
        elif t == "value_item_confirmed":
            verdict = metrics_sync.VERDICTS.get(new.get("verdict"), new.get("verdict"))
            text = f"Вердикт по статті «{title}»: {verdict.lower()}, «{new.get('comment')}»"
        elif t == "value_confirmation_reset":
            text = f"Вердикт по статті «{title}» скинуто: {new.get('reason')}"
        else:
            text = metrics_sync.EVENT_LABELS.get(t, t)
        return {"when": event.created_at, "who": who, "text": text}
    metric = metrics.get(event.metric_key) if event.metric_key else None
    name = metric.name if metric else (event.metric_key or "")
    unit = metric.unit if metric else ""
    old, new = event.old_value, event.new_value
    t = event.event_type
    if t == "sync_ok":
        text = "Синк з GitHub: успішно"
    elif t == "sync_failed":
        text = f"Синк з GitHub: файл метрик відхилено ({len((new or {}).get('errors') or [])} помилок)"
    elif t == "metric_added":
        text = f"Додано метрику: {name or (new or {}).get('name')}"
    elif t == "metric_removed":
        text = f"Метрику прибрано з репо: {name or (old or {}).get('name')}"
    elif t == "measurement_added":
        text = (f"Новий замір: {name}, {_value_with_unit((new or {}).get('value'), unit)} "
                f"за {fmt_period((new or {}).get('period'))}")
    elif t == "measurement_changed_in_repo":
        when = fmt_period((old or {}).get("period"))
        if new is None:
            text = f"Замір видалено в репо: {name}, за {when}. У дашборді збережено"
        else:
            text = (f"Замір змінено в репо: {name}, за {when}: {_value_with_unit(old.get('value'), unit)} → "
                    f"{_value_with_unit(new.get('value'), unit)}. У дашборді лишилось старе значення")
    elif t == "baseline_changed":
        text = f"Змінено «до»: {_short_value(t, old, unit)} → {_short_value(t, new, unit)} ({name})"
    elif t == "target_changed":
        text = f"Змінено ціль: {_short_value(t, old, unit)} → {_short_value(t, new, unit)} ({name})"
    elif t == "method_changed":
        old_every, new_every = (old or {}).get("check_every_days"), (new or {}).get("check_every_days")
        if old_every != new_every:
            def every(d):
                return f"раз на {d} дн." if d else "не задано"
            text = f"Змінено частоту замірів: {every(old_every)} → {every(new_every)} ({name})"
        else:
            text = f"Змінено опис перевірки ({name})"
    elif t == "confirmed":
        verdict = metrics_sync.VERDICTS.get((new or {}).get("verdict"), (new or {}).get("verdict"))
        text = f"Вердикт: {verdict.lower()} ({name}), «{(new or {}).get('comment')}»"
    elif t == "confirmation_reset":
        text = f"Підтвердження скинуто ({name}): {(new or {}).get('reason')}"
    elif t == "roi_field_changed":
        text = (f"Змінено вручну: {ROI_FIELD_LABELS.get(event.field, event.field)}, "
                f"{fmt_number(old) if old is not None else 'не задано'} → "
                f"{fmt_number(new) if new is not None else 'не задано'}")
    else:
        text = metrics_sync.EVENT_LABELS.get(t, t)
    who = SYNC_AUTHOR if event.actor_label == metrics_sync.SYNC_ACTOR else event.actor_label
    return {"when": event.created_at, "who": who, "text": text}


# --- Per-automation rows ------------------------------------------------------

def _trend(direction, baseline, latest):
    if baseline is None or latest is None:
        return None
    if latest == baseline:
        return "same"
    better = latest < baseline if direction == "down" else latest > baseline
    return "better" if better else "worse"


def _money_words(entry, kind):
    metric = entry["metric"]
    money = metric.money if isinstance(metric.money, dict) else None
    gain = entry["improvement"] if kind == "measured" else entry.get("improvement_estimate")
    total = entry["money_measured"] if kind == "measured" else entry["money_estimate"]
    if money is None or gain is None or total is None:
        return None
    return (f"різниця {fmt_number(gain)} × ставка ${fmt_number(money.get('rate_usd_per_unit'))} × "
            f"коефіцієнт {fmt_number(money.get('realization'))} = {fmt_usd(total)}/міс. "
            f"Чому такий коефіцієнт: {money.get('realization_why') or 'не пояснено'}")


def _confirmation_dict(c):
    """A MetricConfirmation or ValueItemConfirmation for display, or None."""
    if c is None:
        return None
    return {"who": c.confirmed_by.name if c.confirmed_by else "невідомо", "when": c.confirmed_at,
            "verdict": c.verdict, "verdict_label": metrics_sync.VERDICTS.get(c.verdict, c.verdict),
            "comment": c.comment, "reset_reason": c.invalidated_reason}


def _confirmation_info(automation_id, metric_key):
    return _confirmation_dict(metrics_sync.latest_confirmation(automation_id, metric_key))


def _tile_key(entry):
    """Which first-row tile the primary metric lands in. "Підтверджено"
    holds Oksana's "підтверджено" and "частково" verdicts that still hold;
    "не підтверджено", or a verdict a newer measurement made stale, falls
    back to what the data itself says."""
    if entry["status"] == "confirmed" and entry["verdict"] in ("confirmed", "partial"):
        return "confirmed"
    if entry["latest_measured"] is not None:
        return "measured"
    if entry["status"] == "no_data":
        return "no_data"
    return "awaiting"


def _old_tile_key(automation):
    roi = automation.roi
    if roi is None:
        return "no_data"
    if roi.measured_hours_per_month is not None:
        return "measured"
    if roi.estimated_hours_saved_per_month is not None:
        return "awaiting"
    return "no_data"


def _rejected_snapshot(automation_id):
    """The latest snapshot if it was rejected - shown as a badge with its
    errors while the page keeps using the last valid snapshot's data."""
    latest = (MetricsSnapshot.query.filter_by(automation_id=automation_id)
              .order_by(MetricsSnapshot.fetched_at.desc(), MetricsSnapshot.id.desc()).first())
    if latest is None or latest.is_valid:
        return None
    errors = latest.errors or []
    return {"errors": errors, "human_errors": [humanize_error(e) for e in errors],
            "when": latest.fetched_at, "sha": latest.commit_sha, "hint": REJECTED_HINT}


def _saved_hours(row):
    """(hours/month, measured?) this automation saves, or None - the one
    figure both the big card and the net-balance chart use. metrics.json:
    time metrics in год/міс, a measured result where there is one, else an
    estimate measurement's. Old format: measured_hours_per_month, else the
    Stage 1 estimate."""
    if row["format"] == "metrics":
        measured = estimated = 0.0
        any_measured = any_estimated = False
        for e in row["metrics"]:
            if e["metric"].type != "time" or (e["metric"].unit or "").strip().lower() not in HOURS_PER_MONTH_UNITS:
                continue
            if e["improvement"] is not None:
                measured += e["improvement"]
                any_measured = True
            elif e["improvement_estimate"] is not None:
                estimated += e["improvement_estimate"]
                any_estimated = True
        if not (any_measured or any_estimated):
            return None
        return {"measured": measured, "estimated": estimated, "total": measured + estimated,
                "is_measured": any_measured and not any_estimated}
    roi = row["automation"].roi
    if roi is None:
        return None
    if roi.measured_hours_per_month is not None:
        value = float(roi.measured_hours_per_month)
        return {"measured": value, "estimated": 0.0, "total": value, "is_measured": True}
    if roi.estimated_hours_saved_per_month is not None:
        value = float(roi.estimated_hours_saved_per_month)
        return {"measured": 0.0, "estimated": value, "total": value, "is_measured": False}
    return None


def _question_subject(q, metric_rows, item_rows):
    if q.get("value_item_id") in item_rows:
        return item_rows[q["value_item_id"]].title
    if q.get("metric_id") in metric_rows:
        return metric_rows[q["metric_id"]].name
    # A question with neither id is general; the one case with a subject
    # anyone can name is at_scale, whose fields are asked exactly that way.
    field = q.get("field")
    if isinstance(field, str) and (field == "at_scale" or field.startswith("at_scale.")):
        return "Повний масштаб"
    return "Загальне"


def _value_status(buckets):
    """Overall status of the money that counts, from the buckets it sits in."""
    if not buckets:
        return None
    if buckets == {"confirmed"}:
        return "confirmed"
    if buckets <= {"confirmed", "measured"}:
        return "measured"
    return "estimate"


# --- The three blocks an expanded row opens with -----------------------------

MAX_FRESH = 3
EVENTS_SHOWN = 5
EVENTS_LOADED = 50


def _money_summary(row):
    """Block 1's headline: "≈ $575/міс, оцінка, не доведено" plus the
    confirmed part under it. Value items when there are any, else the
    metrics' own money; {"headline": None} when nothing is counted yet."""
    if row["value"]:
        v = row["value"]
        totals, seen, net, status = v["totals"], v["seen"], v["net"], v["status"]
    else:
        totals = {"confirmed": 0.0, "measured": 0.0, "estimate": 0.0, "rejected": 0.0}
        seen = {k: False for k in totals}
        for e in row["metrics"]:
            bucket = _money_bucket(e)
            if bucket:
                totals[bucket[0]] += bucket[1]
                seen[bucket[0]] = True
        counted = {k for k in ("confirmed", "measured", "estimate") if seen[k]}
        net = sum(totals[k] for k in counted) if counted else None
        status = _value_status(counted)
    if net is None:
        return {"headline": None, "sub": None, "negative": False}
    if status == "confirmed":
        headline, sub = f"{fmt_usd(net)}/міс, підтверджено", None
    else:
        words = {"measured": "виміряно, не підтверджено", "estimate": "оцінка, не доведено"}[status]
        headline, sub = f"≈ {fmt_usd(net)}/міс, {words}", f"підтверджено {fmt_usd(totals['confirmed'])}"
    if seen["rejected"]:
        sub = (sub + ", " if sub else "") + f"не підтверджено і не в сумі {fmt_usd(totals['rejected'])}"
    return {"headline": headline, "sub": sub, "negative": net < 0}


def _fresh_line(entry):
    """One block-2 row: "61% → 57%", arrow and its tone. Green when the
    number moved the metric's way, red when against it, grey when it moved by
    less than 1 or there is no "before" to compare with."""
    metric = entry["metric"]
    before, after = entry["baseline"], entry["latest_measured"]
    diff = None if before is None else after - before
    if diff is None or abs(diff) < 1:
        tone = "muted"
    else:
        tone = "success" if (diff < 0) == (metric.direction == "down") else "danger"
    arrow = "right" if not diff else ("down" if diff < 0 else "up")
    unit = (metric.unit or "").strip()
    if unit.startswith("%"):
        before_text = _value_with_unit(before, unit) if before is not None else "?"
        after_text = _value_with_unit(after, unit)
    else:
        before_text = fmt_number(before) if before is not None else "?"
        after_text = _value_with_unit(after, unit)
    target = entry["target_value"]
    return {"name": metric.name, "is_primary": metric.is_primary,
            "before_text": before_text, "after_text": after_text, "arrow": arrow, "tone": tone,
            "target_text": f"ціль {_value_with_unit(target, unit)}" if target is not None else "ціль ?",
            "date_text": fmt_date(entry["latest_measured_date"])}


def _is_fresh(entry, today):
    """A measured value after launch that is still within check_every_days of
    its own date: `overdue` alone also counts later estimates as a check."""
    if entry["latest_measured"] is None or entry["overdue"]:
        return False
    every = entry["metric"].check_every_days
    return not every or entry["latest_measured_date"] + datetime.timedelta(days=every) >= today


def _fresh_block(metrics, today):
    """Block 2: up to MAX_FRESH metrics with a fresh measurement, primary
    first; else one line about the next one due."""
    fresh = [e for e in metrics if _is_fresh(e, today)]
    fresh.sort(key=lambda e: not e["metric"].is_primary)
    lines = [_fresh_line(e) for e in fresh[:MAX_FRESH]]
    if lines:
        return {"lines": lines, "empty_text": None}
    due = min((e["next_due"] for e in metrics if e["next_due"]), default=None)
    if due is None:
        text = "Свіжих замірів немає"
    elif due < today:
        text = f"Свіжих замірів немає, замір прострочено з {fmt_date(due)}"
    else:
        text = f"Свіжих замірів немає, наступний до {fmt_date(due)}"
    return {"lines": [], "empty_text": text}


def _question_groups(questions):
    """Block 3: open questions grouped by who to ask, biggest group first."""
    groups = {}
    for q in questions:
        groups.setdefault(q["ask_whom_label"], []).append({"subject": q["metric_name"], "question": q["question"]})
    ordered = sorted(groups.items(), key=lambda g: -len(g[1]))
    return [{"who": who, "count": len(items), "items": items} for who, items in ordered]


def build_value(automation, questions):
    """Value items of one automation for display, plus their totals - None
    for an automation without any (the row then looks exactly as before)."""
    entries = metrics_sync.automation_value_items(automation)
    if not entries:
        return None
    items = []
    totals = {"confirmed": 0.0, "measured": 0.0, "estimate": 0.0, "rejected": 0.0}
    seen = {k: False for k in totals}
    for e in entries:
        row = e["item"]
        asked = {}
        for q in questions:
            if q.get("value_item_id") == row.item_key and q.get("field"):
                asked.setdefault(q["field"], q["ask_whom_label"])
        source, tech = split_source(row.source) if row.source else ("", "")
        pill_kind, pill_class = CATEGORY_PILL.get(row.category, ("badge", "bg-light-secondary"))
        items.append({
            "key": row.item_key, "category": row.category,
            "category_label": CATEGORIES.get(row.category, row.category),
            "pill_kind": pill_kind, "pill_class": pill_class, "title": row.title,
            "usd": row.usd_per_month, "usd_text": fmt_usd(row.usd_per_month) if row.usd_per_month is not None else None,
            "status": e["status"], "label": e["label"], "badge": VALUE_STATUS_BADGE[e["status"]],
            "basis_label": BASIS_LABELS.get(row.basis, row.basis), "formula": row.formula, "why": row.why,
            "source": source, "source_tech": tech, "source_full": row.source or "",
            "as_of_text": fmt_date(row.as_of), "asked": asked, "is_run_cost": row.category == "run_cost",
            "confirmation": _confirmation_dict(e["confirmation"]),
        })
        if row.usd_per_month is not None and e["bucket"]:
            totals[e["bucket"]] += row.usd_per_month
            seen[e["bucket"]] = True
    counted = [i for i, e in zip(items, entries) if i["usd"] is not None and e["bucket"] != "rejected"]
    status = _value_status({e["bucket"] for i, e in zip(items, entries)
                            if i["usd"] is not None and e["bucket"] != "rejected"})
    categories = []
    for code in CATEGORIES:
        if any(i["category"] == code for i in items):
            kind, cls = CATEGORY_PILL[code]
            categories.append({"label": CATEGORIES[code], "pill_kind": kind, "pill_class": cls})
    return {
        "items": items,
        "totals": totals,
        "seen": seen,
        "net": (totals["confirmed"] + totals["measured"] + totals["estimate"]) if counted else None,
        "status": status,
        "status_label": metrics_sync.VALUE_STATUS_LABELS[status] if status else None,
        "status_badge": VALUE_STATUS_BADGE[status] if status else None,
        "categories": categories,
    }


def _scale_amount(value):
    """Display only finite, representable amounts; keep snapshots untouched."""
    if not is_number(value):
        return None
    try:
        amount = float(value)
    except (OverflowError, ValueError):
        return None
    return amount if math.isfinite(amount) else None


def build_at_scale(at_scale, questions):
    """Contract v1.1's `at_scale` for display - what the automation would be
    worth at the agreed target volume, while it still runs on a small one.
    None when the file has no such block (then the row looks as before).

    The money here never joins a sum on this page - not the row's own
    "Цінність", not the "Гроші" tile, not "Разом" in the details: it's an
    estimate for a volume the automation doesn't run on yet, and mixing it
    with current money would make the portfolio look several times richer
    than it is. Hence AT_SCALE_NOTE wherever the figure shows."""
    if not isinstance(at_scale, dict):
        return None
    low = _scale_amount(at_scale.get("usd_per_month_low"))
    high = _scale_amount(at_scale.get("usd_per_month_high"))
    bad_amount = any(is_number(at_scale.get(key)) and amount is None
                     for key, amount in (("usd_per_month_low", low), ("usd_per_month_high", high)))
    if bad_amount:
        low = high = None
    # A high equal to the low is the contract's way of writing a single
    # figure; showing "~$4 976–$4 976" would read as a range that isn't one.
    if low is not None and high is not None and round(high) == round(low):
        high = None
    if low is None:
        sum_text = None
    elif high is None:
        sum_text = f"~{fmt_usd(low)}"
    else:
        sum_text = f"~{fmt_usd(low)}–{fmt_usd(high)}"

    asked = {}
    for q in questions:
        if q.get("metric_id") is not None or q.get("value_item_id") is not None:
            continue
        field = q.get("field")
        if not isinstance(field, str):
            continue
        if field == "at_scale":  # one question about the block covers every field
            for key in AT_SCALE_FIELDS:
                asked.setdefault(key, q["ask_whom_label"])
        elif field.startswith("at_scale."):
            asked.setdefault(field.split(".", 1)[1], q["ask_whom_label"])

    # The reference validator permits non-string descriptions. Keep that
    # contract, but never pass malformed snapshot data to regex/text helpers.
    text = {key: value if isinstance(value := at_scale.get(key), str) else None
            for key in ("volume", "condition", "formula", "source")}
    issues = (["Перевірте дані повного масштабу: описові поля мають бути текстом."]
              if any(at_scale.get(key) is not None and not isinstance(at_scale[key], str)
                     for key in text) else [])
    if bad_amount:
        issues.append("Перевірте дані повного масштабу: суму неможливо показати як коректне число.")
    source_full = text["source"] or ""
    source, tech = split_source(source_full) if source_full else ("", "")
    volume, condition = text["volume"], text["condition"]
    hint = "\n".join(part for part in (f"Обсяг: {volume}" if volume else "",
                                       f"Умова: {condition}" if condition else "") if part)
    return {
        "low": low, "high": high, "sum_text": sum_text,
        # The narrow column line: the sum only, with the volume and the
        # condition behind a tooltip instead of three more lines per row.
        "column_text": f"на повному масштабі {sum_text}" if sum_text else None,
        "hint": hint or None,
        "note": AT_SCALE_NOTE,
        "volume": volume, "condition": condition, "formula": text["formula"],
        "source": source, "source_tech": tech, "source_full": source_full,
        "issues": issues,
        "as_of_text": fmt_date(at_scale.get("as_of")),
        "asked": asked,
    }


def build_row(automation, today):
    overview = metrics_sync.automation_metrics(automation, today=today)
    metric_rows = {m.metric_key: m for m in AutomationMetric.query.filter_by(automation_id=automation.id)}
    item_rows = {r.item_key: r for r in AutomationValueItem.query.filter_by(automation_id=automation.id)}
    events = (AutomationEvent.query.filter_by(automation_id=automation.id)
              .order_by(AutomationEvent.created_at.desc(), AutomationEvent.id.desc()).limit(EVENTS_LOADED).all())
    questions = [
        dict(q, metric_name=_question_subject(q, metric_rows, item_rows),
             field_label=field_label(q.get("field")), ask_whom_label=ask_whom_label(q.get("ask_whom")),
             since_text=fmt_date(q.get("since")))
        for q in overview["open_questions"]]
    row = {
        "automation": automation,
        "format": overview["format"],
        "launched_at": overview["launched_at"],
        "rejected": _rejected_snapshot(automation.id),
        "events": [describe_event(e, metric_rows, item_rows) for e in events],
        "open_questions": questions,
        "metrics": [],
        "primary": None,
        "others": [],
        "snapshot_id": None,
        "snapshot_sha": None,
        "value": None,
        "at_scale": None,
        "question_groups": _question_groups(questions),
    }
    if overview["format"] == "metrics":
        snapshot = metrics_sync.latest_valid_snapshot(automation.id)
        row["snapshot_id"], row["snapshot_sha"] = snapshot.id, snapshot.commit_sha
        row["value"] = build_value(automation, questions)
        row["at_scale"] = build_at_scale(overview.get("at_scale"), questions)
        for entry in overview["metrics"]:
            metric = entry["metric"]
            baseline_value = entry["baseline"]
            estimate = next((m for m in reversed(entry["measurements"])
                             if m["kind"] == "estimate" and overview["launched_at"]
                             and m["date"] > overview["launched_at"]), None)
            estimate_value = float(estimate["value"]) if estimate and is_number(estimate["value"]) else None
            entry["improvement_estimate"] = (
                None if baseline_value is None or estimate_value is None
                else (baseline_value - estimate_value if metric.direction == "down" else estimate_value - baseline_value))
            entry["target_value"] = metric.target.get("value") if isinstance(metric.target, dict) else None
            entry["trend"] = _trend(metric.direction, baseline_value, entry["latest_measured"])
            entry["confirmation"] = _confirmation_info(automation.id, metric.metric_key)
            entry["baseline_parts"] = _fmt_baseline_parts(metric.baseline)
            entry["money_measured_words"] = _money_words(entry, "measured")
            entry["money_estimate_words"] = _money_words(entry, "estimate")
            entry["type_label"] = TYPE_LABELS.get(metric.type, metric.type)
            entry["type_pill"] = TYPE_PILL_CLASS.get(metric.type)
            # field -> who to ask, for "не задано, питання до: ..." in place
            # of a bare "немає даних" wherever someone is already on it.
            entry["asked"] = {}
            for q in questions:
                if q.get("metric_id") == metric.metric_key and q.get("value_item_id") is None and q.get("field"):
                    entry["asked"].setdefault(q["field"].split(".")[0], q["ask_whom_label"])
            entry["history"] = [
                dict(m, date_text=fmt_date(m["date"]), period_text=fmt_period(m["period"]),
                     value_text=_value_with_unit(m["value"], metric.unit),
                     source_text=split_source(m.get("source"))[0], source_tech=split_source(m.get("source"))[1])
                for m in reversed(entry["measurements"])]
            row["metrics"].append(entry)
        row["primary"] = next((e for e in row["metrics"] if e["metric"].is_primary), None)
        row["others"] = [e for e in row["metrics"] if e is not row["primary"]]
        row["tile"] = _tile_key(row["primary"]) if row["primary"] else "no_data"
        row["has_measured"] = bool(row["primary"] and row["primary"]["latest_measured"] is not None)
        row["money_summary"] = _money_summary(row)
        row["fresh"] = _fresh_block(row["metrics"], today)
    else:
        row["tile"] = _old_tile_key(automation)
        row["has_measured"] = row["tile"] == "measured"
    row["saved_hours"] = _saved_hours(row)
    return row


# --- Page totals ------------------------------------------------------------

def _money_bucket(entry):
    """Which "Гроші" line a metric's money goes to - None for a metric with
    no money at all. A "не підтверджено" verdict is shown but never summed."""
    if entry["money_measured"] is not None:
        if entry["status"] == "confirmed" and entry["verdict"] == "rejected":
            return "rejected", entry["money_measured"]
        if entry["status"] == "confirmed" and entry["verdict"] == "confirmed":
            return "confirmed", entry["money_measured"]
        return "measured", entry["money_measured"]
    if entry["money_estimate"] is not None:
        return "estimate", entry["money_estimate"]
    return None


def build_context(automations, today=None):
    today = today or datetime.date.today()
    rows = [build_row(a, today) for a in automations]

    saved = [r["saved_hours"] for r in rows if r["saved_hours"] is not None]
    saved_measured = sum(s["measured"] for s in saved)
    saved_estimated = sum(s["estimated"] for s in saved)

    tiles = {k: sum(1 for r in rows if r["tile"] == k) for k in TILE_ORDER}
    confirmed_rows = [r for r in rows if r["tile"] == "confirmed"]
    measured_rows = [r for r in rows if r["tile"] == "measured"]
    partial_n = sum(1 for r in confirmed_rows if r["primary"] and r["primary"]["verdict"] == "partial")
    rejected_verdict_n = sum(1 for r in measured_rows if r["primary"] and r["primary"]["status"] == "confirmed"
                             and r["primary"]["verdict"] == "rejected")
    old_format_n = sum(1 for r in rows if r["format"] == "old")
    measured_auto_n = sum(1 for r in rows if r["has_measured"])

    money = {"confirmed": 0.0, "measured": 0.0, "estimate": 0.0, "rejected": 0.0}
    money_seen = {k: False for k in money}
    for row in rows:
        # Value items are where an automation's money lives once it has them
        # (the contract forbids metric.money next to them); never both.
        if row["value"]:
            for key in money:
                if row["value"]["seen"][key]:
                    money[key] += row["value"]["totals"][key]
                    money_seen[key] = True
            continue
        for e in row["metrics"]:
            bucket = _money_bucket(e)
            if bucket:
                money[bucket[0]] += bucket[1]
                money_seen[bucket[0]] = True
    has_money = any(money_seen.values())
    money_total = (money["confirmed"] + money["measured"] + money["estimate"]
                   if any(money_seen[k] for k in ("confirmed", "measured", "estimate")) else None)

    # Separate from money_total above and never folded into it: the lower
    # bound of what the automations that have an at_scale block would be
    # worth at their target volume. The low end only - the tile is one line,
    # and the cautious end of an estimate is the one worth quoting.
    at_scale_lows = [r["at_scale"]["low"] for r in rows
                     if r["at_scale"] and r["at_scale"]["low"] is not None]
    at_scale_total = _scale_amount(sum(at_scale_lows)) if at_scale_lows else None
    at_scale_n = len(at_scale_lows)

    type_counts = {k: 0 for k in TYPE_ORDER}
    for row in rows:
        if row["format"] == "metrics":
            types = {e["metric"].type for e in row["metrics"]}
        else:
            types = {STAGE1_TO_TYPE[t] for t in target_metrics(row["automation"])}
        for t in types:
            if t in type_counts:
                type_counts[t] += 1
    max_count = max(type_counts.values()) if type_counts else 0
    metric_counts = [
        {"label": TYPE_LABELS[k], "css_var": TYPE_CSS_VAR[k], "count": type_counts[k],
         "pct": round(type_counts[k] / max_count * 100) if max_count else 0}
        # The four Stage 1 types always show (a zero bar is information);
        # revenue/other only once some metric actually uses them.
        for k in TYPE_ORDER if type_counts[k] or k in STAGE1_TO_TYPE.values()
    ]

    devs = [a.roi.dev_hours for a in automations if a.roi and a.roi.dev_hours is not None]
    maints = [a.roi.maintenance_hours_per_month for a in automations
              if a.roi and a.roi.maintenance_hours_per_month is not None]
    with_subs = [a for a in automations if a.monthly_subscription_cost_usd is not None]
    subs_cost_total = sum((a.monthly_subscription_cost_usd for a in with_subs), start=0)
    linked_sub_ids = {s.id for a in automations for s in a.subscriptions}

    # Net balance: the same saved-hours figure as the big card, minus the
    # ongoing maintenance hours - None (no bar) unless both sides are known.
    net_rows = []
    for row in rows:
        a = row["automation"]
        if row["saved_hours"] is None or a.roi is None or a.roi.maintenance_hours_per_month is None:
            continue
        net_rows.append({
            "automation": a,
            "net": row["saved_hours"]["total"] - float(a.roi.maintenance_hours_per_month),
            "measured": row["saved_hours"]["is_measured"],
        })
    net_rows.sort(key=lambda r: r["net"], reverse=True)
    net_scale = max([10.0] + [abs(r["net"]) for r in net_rows])

    return {
        "rows": rows,
        "saved_total": saved_measured + saved_estimated if saved else None,
        "saved_measured": saved_measured,
        "saved_estimated": saved_estimated,
        "total_n": len(automations),
        "tiles": tiles,
        "partial_n": partial_n,
        "rejected_verdict_n": rejected_verdict_n,
        "old_format_n": old_format_n,
        "confidence_pct": round(measured_auto_n / len(rows) * 100) if rows else None,
        "measured_auto_n": measured_auto_n,
        "has_money": has_money,
        "money_total": money_total,
        "money": money,
        "money_seen": money_seen,
        "at_scale_total": at_scale_total,
        "at_scale_n": at_scale_n,
        "dev_total": float(sum(devs, start=0)) if devs else None,
        "dev_n": len(devs),
        "maint_total": float(sum(maints, start=0)) if maints else None,
        "maint_n": len(maints),
        "subs_cost_total": float(subs_cost_total) if with_subs else None,
        "subs_linked_count": len(linked_sub_ids),
        "metric_counts": metric_counts,
        "net_rows": net_rows,
        "net_scale": net_scale,
    }
