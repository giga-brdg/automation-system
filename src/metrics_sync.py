"""Everything the dashboard does with an automation's dashboard/metrics.json
once it's fetched: keep an immutable snapshot of every sync, apply a valid
one to the current metric descriptions, append measurements (never update
or delete them), journal every change, handle an admin's confirmation, and
work out each metric's status for display.

Validation itself is src/metrics_contract.py; this module is the part that
touches the database. No Flask request objects in here - routes in app.py
pass the acting user in, so these functions are callable from the CLI
org-wide sync (no user) and from tests alike."""
import datetime

from .extensions import db
from .metrics_contract import is_number, overdue_date, parse_and_validate, parse_date
from .models import (
    AutomationEvent,
    AutomationMetric,
    MetricConfirmation,
    MetricMeasurement,
    MetricsSnapshot,
    _now,
)

SYNC_ACTOR = "github sync"

EVENT_LABELS = {
    "sync_ok": "Синк успішний",
    "sync_failed": "Синк з помилкою",
    "metric_added": "Метрику додано",
    "metric_removed": "Метрику прибрано",
    "baseline_changed": "Змінено «до»",
    "target_changed": "Змінено ціль",
    "method_changed": "Змінено метод або частоту",
    "measurement_added": "Новий замір",
    "measurement_changed_in_repo": "Замір змінено або видалено в репо",
    "confirmed": "Підтверджено",
    "confirmation_reset": "Підтвердження скинуто",
    "roi_field_changed": "Змінено поле ROI вручну",
}

VERDICTS = {
    "confirmed": "Підтверджено",
    "partial": "Частково",
    "rejected": "Не підтверджено",
}

STATUS_LABELS = {
    "no_data": "Немає даних",
    "awaiting": "Очікуємо замір",
    "measured": "Виміряно",
    "confirmed": "Підтверджено",
    "needs_review": "Потребує перевірки",
}

# ROIEntry columns whose manual edits get journaled (see record_roi_changes).
ROI_FORM_FIELDS = ("measured_hours_per_month", "confidence", "dev_hours", "maintenance_hours_per_month")
ROI_STAGE1_FIELDS = ("baseline_cycle_minutes", "baseline_frequency_per_month",
                     "target_cycle_minutes", "target_frequency_per_month")


def log_event(automation, event_type, actor=None, actor_label=None, metric_key=None, field=None,
              old_value=None, new_value=None, commit_sha=None):
    event = AutomationEvent(
        automation_id=automation.id, event_type=event_type,
        actor_id=actor.id if actor is not None else None,
        actor_label=actor_label or (actor.name if actor is not None else SYNC_ACTOR),
        metric_key=metric_key, field=field, old_value=old_value, new_value=new_value,
        commit_sha=commit_sha)
    db.session.add(event)
    return event


# --- Applying a synced file -------------------------------------------------

_DESCRIPTIVE_FIELDS = ("name", "type", "unit", "direction")


def _measurement_payload(row):
    return {"date": row.date.isoformat(), "period": row.period, "value": row.value,
            "source": row.source, "kind": row.kind, "note": row.note}


def _same_measurement(row, entry):
    return (row.value == entry.get("value") and row.source == entry.get("source")
            and row.kind == entry.get("kind") and (row.note or "") == (entry.get("note") or ""))


def _already_logged(existing_events, event_type, metric_key, old_value, new_value):
    return any(e.event_type == event_type and e.metric_key == metric_key
               and e.old_value == old_value and e.new_value == new_value for e in existing_events)


def apply_metrics_file(automation, text, commit_sha=None, actor=None, roi_md=None, today=None):
    """Records a snapshot of `text` (dashboard/metrics.json as fetched) and,
    if it passes the contract, applies it. Returns user-facing warnings for
    the sync's flash message. An invalid file changes nothing but the
    snapshot and the journal: previously stored metrics stay as they were.
    The caller commits."""
    if automation.id is None:
        db.session.flush()
    data, errors, warnings = parse_and_validate(text, today=today, roi_md=roi_md)
    snapshot = MetricsSnapshot(
        automation_id=automation.id, commit_sha=commit_sha, raw_json=text,
        errors=errors, warnings=warnings, is_valid=not errors,
        triggered_by_id=actor.id if actor is not None else None)
    db.session.add(snapshot)
    db.session.flush()

    if errors:
        log_event(automation, "sync_failed", actor, new_value={"errors": errors}, commit_sha=commit_sha)
        shown = "; ".join(errors[:3]) + (f" (і ще {len(errors) - 3})" if len(errors) > 3 else "")
        return [f"dashboard/metrics.json не прийнято, метрики лишились попередні. Помилки: {shown}"]

    metrics = {m.metric_key: m for m in AutomationMetric.query.filter_by(automation_id=automation.id)}
    seen = set()
    for item in data["metrics"]:
        key = item["id"]
        seen.add(key)
        metric = metrics.get(key)
        if metric is None:
            metric = AutomationMetric(automation_id=automation.id, metric_key=key)
            db.session.add(metric)
            metrics[key] = metric
            log_event(automation, "metric_added", actor, metric_key=key,
                      new_value={"name": item["name"]}, commit_sha=commit_sha)
        else:
            if not metric.is_active:
                log_event(automation, "metric_added", actor, metric_key=key,
                          new_value={"name": item["name"]}, commit_sha=commit_sha)
            if metric.baseline != item["baseline"]:
                log_event(automation, "baseline_changed", actor, metric_key=key, field="baseline",
                          old_value=metric.baseline, new_value=item["baseline"], commit_sha=commit_sha)
            if metric.target != item["target"]:
                log_event(automation, "target_changed", actor, metric_key=key, field="target",
                          old_value=metric.target, new_value=item["target"], commit_sha=commit_sha)
            old_plan = {"method": metric.method, "check_every_days": metric.check_every_days}
            new_plan = {"method": item["method"], "check_every_days": item["check_every_days"]}
            if old_plan != new_plan:
                log_event(automation, "method_changed", actor, metric_key=key, field="method",
                          old_value=old_plan, new_value=new_plan, commit_sha=commit_sha)
        for field in _DESCRIPTIVE_FIELDS:
            setattr(metric, field, item[field])
        metric.is_primary = item["primary"]
        metric.baseline = item["baseline"]
        metric.target = item["target"]
        metric.method = item["method"]
        metric.check_every_days = item["check_every_days"]
        metric.money = item.get("money")
        metric.is_active = True
        metric.last_snapshot_id = snapshot.id

    for key, metric in metrics.items():
        if key not in seen and metric.is_active:
            metric.is_active = False
            log_event(automation, "metric_removed", actor, metric_key=key,
                      old_value={"name": metric.name}, commit_sha=commit_sha)

    _apply_measurements(automation, data, snapshot, actor, commit_sha)
    db.session.flush()
    _reset_stale_confirmations(automation, actor, commit_sha)
    log_event(automation, "sync_ok", actor, commit_sha=commit_sha,
              new_value={"metrics": len(data["metrics"]), "open_questions": len(data["open_questions"])})
    return [f"dashboard/metrics.json: {w}" for w in warnings]


def _apply_measurements(automation, data, snapshot, actor, commit_sha):
    stored = {(r.metric_key, r.date, r.period): r
              for r in MetricMeasurement.query.filter_by(automation_id=automation.id)}
    past_events = AutomationEvent.query.filter_by(
        automation_id=automation.id, event_type="measurement_changed_in_repo").all()
    incoming = set()
    for item in data["metrics"]:
        for entry in item["measurements"]:
            key = (item["id"], parse_date(entry["date"]), entry["period"])
            incoming.add(key)
            row = stored.get(key)
            if row is None:
                row = MetricMeasurement(
                    automation_id=automation.id, metric_key=item["id"], date=key[1], period=entry["period"],
                    value=entry["value"], source=entry["source"], kind=entry["kind"], note=entry.get("note"),
                    first_snapshot_id=snapshot.id)
                db.session.add(row)
                stored[key] = row
                log_event(automation, "measurement_added", actor, metric_key=item["id"],
                          new_value=_measurement_payload(row), commit_sha=commit_sha)
            elif not _same_measurement(row, entry):
                old, new = _measurement_payload(row), dict(entry)
                if not _already_logged(past_events, "measurement_changed_in_repo", item["id"], old, new):
                    log_event(automation, "measurement_changed_in_repo", actor, metric_key=item["id"],
                              old_value=old, new_value=new, commit_sha=commit_sha)
    for key, row in stored.items():
        if key not in incoming:
            old = _measurement_payload(row)
            if not _already_logged(past_events, "measurement_changed_in_repo", key[0], old, None):
                log_event(automation, "measurement_changed_in_repo", actor, metric_key=key[0],
                          old_value=old, new_value=None, commit_sha=commit_sha)


# --- Confirmation -----------------------------------------------------------

def _measurement_keys(automation_id, metric_key):
    rows = MetricMeasurement.query.filter_by(automation_id=automation_id, metric_key=metric_key)
    return sorted(f"{r.date.isoformat()}|{r.period}" for r in rows)


def _current_state(metric):
    return {"baseline": metric.baseline, "target": metric.target,
            "measurements": _measurement_keys(metric.automation_id, metric.metric_key)}


def _state_change(confirmed_state, current):
    """Why a confirmation no longer holds, or None if it still does. Only a
    new measurement counts on the measurement side - an old one changed in
    the repo is kept as it was here, so it can't move the result."""
    confirmed_state = confirmed_state or {}
    if set(current["measurements"]) - set(confirmed_state.get("measurements") or []):
        return "новий замір"
    if current["baseline"] != confirmed_state.get("baseline"):
        return "змінено «до»"
    if current["target"] != confirmed_state.get("target"):
        return "змінено ціль"
    return None


def latest_confirmation(automation_id, metric_key):
    return (MetricConfirmation.query.filter_by(automation_id=automation_id, metric_key=metric_key)
            .order_by(MetricConfirmation.confirmed_at.desc(), MetricConfirmation.id.desc()).first())


def _reset_stale_confirmations(automation, actor, commit_sha):
    for metric in AutomationMetric.query.filter_by(automation_id=automation.id):
        confirmation = latest_confirmation(automation.id, metric.metric_key)
        if confirmation is None or confirmation.invalidated_at is not None:
            continue
        reason = _state_change(confirmation.confirmed_state, _current_state(metric))
        if reason:
            confirmation.invalidated_at = _now()
            confirmation.invalidated_reason = reason
            log_event(automation, "confirmation_reset", actor, metric_key=metric.metric_key,
                      old_value={"verdict": confirmation.verdict, "confirmation_id": confirmation.id},
                      new_value={"reason": reason}, commit_sha=commit_sha)


class ConfirmationError(ValueError):
    pass


def confirm_metric(automation, metric_key, snapshot_id, verdict, comment, user):
    """Records an admin's verdict on `metric_key` as of `snapshot_id`.
    Raises PermissionError for a non-admin and ConfirmationError for
    anything else the caller should show back to the user. Caller commits."""
    if user is None or not user.is_admin:
        raise PermissionError("Підтверджувати результат може лише адміністратор.")
    comment = (comment or "").strip()
    if not comment:
        raise ConfirmationError("Коментар обов'язковий: напиши, на чому ґрунтується вердикт.")
    if verdict not in VERDICTS:
        raise ConfirmationError("Невідомий вердикт.")
    metric = AutomationMetric.query.filter_by(automation_id=automation.id, metric_key=metric_key).first()
    if metric is None:
        raise ConfirmationError("Такої метрики в цієї автоматизації немає.")
    snapshot = db.session.get(MetricsSnapshot, snapshot_id) if snapshot_id else None
    if snapshot is None or snapshot.automation_id != automation.id:
        raise ConfirmationError("Знімок не знайдено.")
    if not snapshot.is_valid:
        raise ConfirmationError("Цей знімок не пройшов перевірку, підтверджувати на ньому не можна.")
    item = next((m for m in snapshot.data["metrics"] if m["id"] == metric_key), None)
    if item is None:
        raise ConfirmationError("У цьому знімку такої метрики немає.")
    # Every measurement the dashboard already knew as of that snapshot, not
    # just the ones still in its file: one the repo later dropped stays
    # stored here, and mustn't read as "new" on the next sync.
    known = {f"{m['date']}|{m['period']}" for m in item["measurements"]}
    known |= {f"{r.date.isoformat()}|{r.period}" for r in MetricMeasurement.query.filter(
        MetricMeasurement.automation_id == automation.id, MetricMeasurement.metric_key == metric_key,
        MetricMeasurement.first_snapshot_id <= snapshot.id)}
    state = {"baseline": item["baseline"], "target": item["target"], "measurements": sorted(known)}
    confirmation = MetricConfirmation(
        automation_id=automation.id, metric_key=metric_key, snapshot_id=snapshot.id, verdict=verdict,
        comment=comment, confirmed_by_id=user.id, confirmed_state=state)
    db.session.add(confirmation)
    log_event(automation, "confirmed", user, metric_key=metric_key,
              new_value={"verdict": verdict, "comment": comment, "snapshot_id": snapshot.id},
              commit_sha=snapshot.commit_sha)
    return confirmation


# --- Status -----------------------------------------------------------------

def _num(value):
    return float(value) if is_number(value) else None


def _improvement(direction, baseline, latest):
    if baseline is None or latest is None:
        return None
    return baseline - latest if direction == "down" else latest - baseline


def _money(money, improvement):
    if not isinstance(money, dict) or improvement is None:
        return None
    rate, realization = _num(money.get("rate_usd_per_unit")), _num(money.get("realization"))
    if rate is None or realization is None:
        return None
    return improvement * rate * realization


def _latest_after_launch(measurements, launched_at, kind):
    if launched_at is None:
        return None
    candidates = [m for m in measurements if m["kind"] == kind and m["date"] > launched_at]
    return max(candidates, key=lambda m: m["date"]) if candidates else None


def compute_status(metric, measurements, launched_at, today, confirmation=None):
    """Pure status calculation for one metric - the single function /value
    and the automation page both read. `metric` is a dict with the contract
    fields (baseline, target, direction, check_every_days, money),
    `measurements` a list of {date: date, period, value, kind, ...},
    `confirmation` {verdict, invalidated, state} or None. Missing data comes
    back as None, never as 0."""
    baseline = metric.get("baseline") or None
    baseline_value = _num(baseline.get("value")) if isinstance(baseline, dict) else None
    measured = _latest_after_launch(measurements, launched_at, "measured")
    estimate = _latest_after_launch(measurements, launched_at, "estimate")

    if confirmation is not None:
        current = {"baseline": metric.get("baseline"), "target": metric.get("target"),
                   "measurements": sorted(f"{m['date'].isoformat()}|{m['period']}" for m in measurements)}
        stale = confirmation.get("invalidated") or _state_change(confirmation.get("state"), current)
        status = "needs_review" if stale else "confirmed"
    elif measured is not None:
        status = "measured"
    elif baseline is None and not measurements:
        status = "no_data"
    else:
        status = "awaiting"

    due = overdue_date(launched_at, metric.get("check_every_days"), [m["date"] for m in measurements])
    measured_value = _num(measured["value"]) if measured else None
    estimate_value = _num(estimate["value"]) if estimate else None
    measured_gain = _improvement(metric.get("direction"), baseline_value, measured_value)
    estimate_gain = _improvement(metric.get("direction"), baseline_value, estimate_value)
    return {
        "status": status,
        "label": STATUS_LABELS[status],
        "verdict": confirmation.get("verdict") if confirmation else None,
        "overdue": bool(due and due < today),
        "next_due": due,
        "baseline": baseline_value,
        "latest_measured": measured_value,
        "latest_measured_date": measured["date"] if measured else None,
        "improvement": measured_gain,
        "money_measured": _money(metric.get("money"), measured_gain),
        "money_estimate": _money(metric.get("money"), estimate_gain),
    }


def latest_valid_snapshot(automation_id):
    return (MetricsSnapshot.query.filter_by(automation_id=automation_id, is_valid=True)
            .order_by(MetricsSnapshot.fetched_at.desc(), MetricsSnapshot.id.desc()).first())


def automation_metrics(automation, today=None):
    """{"format": "old"|"metrics", "launched_at", "open_questions", "metrics": [...]}
    for one automation. "old" means no valid metrics.json has ever been
    synced - the caller falls back to ROIEntry exactly as before."""
    today = today or datetime.date.today()
    snapshot = latest_valid_snapshot(automation.id)
    if snapshot is None:
        return {"format": "old", "launched_at": None, "open_questions": [], "metrics": []}
    data = snapshot.data
    launched_at = parse_date(data.get("launched_at"))
    rows = (AutomationMetric.query.filter_by(automation_id=automation.id, is_active=True)
            .order_by(AutomationMetric.is_primary.desc(), AutomationMetric.id).all())
    result = []
    for metric in rows:
        measurements = [
            {"date": r.date, "period": r.period, "value": r.value, "source": r.source,
             "kind": r.kind, "note": r.note}
            for r in MetricMeasurement.query.filter_by(automation_id=automation.id, metric_key=metric.metric_key)
            .order_by(MetricMeasurement.date)]
        confirmation = latest_confirmation(automation.id, metric.metric_key)
        confirmation_info = None if confirmation is None else {
            "verdict": confirmation.verdict, "invalidated": confirmation.invalidated_at is not None,
            "state": confirmation.confirmed_state}
        fields = {"baseline": metric.baseline, "target": metric.target, "direction": metric.direction,
                  "check_every_days": metric.check_every_days, "money": metric.money}
        result.append({"metric": metric, "measurements": measurements,
                       **compute_status(fields, measurements, launched_at, today, confirmation_info)})
    return {"format": "metrics", "launched_at": launched_at,
            "open_questions": data.get("open_questions") or [], "metrics": result}


# --- Manual ROI edits -------------------------------------------------------

def _normalize(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def roi_values(roi, fields):
    """Snapshot of ROIEntry `fields` before a form overwrites them."""
    return {f: _normalize(getattr(roi, f, None)) if roi is not None else None for f in fields}


def record_roi_changes(automation, before, actor):
    """Journals every field in `before` whose value changed on automation.roi,
    old and new, attributed to `actor`. Call after the form applied its
    values and the automation has an id."""
    after = roi_values(automation.roi, before.keys())
    for field, old in before.items():
        if after[field] != old:
            log_event(automation, "roi_field_changed", actor, field=field,
                      old_value=old, new_value=after[field])
