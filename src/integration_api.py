"""Read-only integration API for Supplax Dashboard (contracts automation.*, api_version 1.x).

Supplax Dashboard shows this portfolio inside its own shell (its users, its rights, its design system). This
module is the ONLY thing it talks to:

    GET /api/integration/v1/automation/<resource>      Authorization: Bearer <INTEGRATION_API_TOKEN>

Rules (Supplax integration CONTRACT, envelope §2):
  * read-only: no route here writes to the DB, calls GitHub / Telegram, or triggers a sync;
  * one service token (env INTEGRATION_API_TOKEN, >= 32 chars), compared in constant time; unset -> every route
    answers 503 integration_disabled (nothing is exposed by default);
  * never a password hash, an api_key or a registration code in any answer; an e-mail only in /people (the work
    e-mail Supplax matches against its own users, server-side, to decide who sees which automations);
  * `only=<slug,slug,...>` (value, automations) restricts the answer to those automations - every total, option
    and count is then computed over that set by the same code (Supplax sends the viewer's allowed set);
  * every number is computed by the SAME code as the HTML pages (value_dashboard.build_context,
    metrics_sync, the model properties) - this module only serialises it. Next to the raw number the answer
    carries the text the HTML page would print (fmt_usd / fmt_hours / fmt_number / fmt_date), so a consumer shows
    exactly the same figures (Python rounds half to even - JS would not);
  * "no data" stays null, never 0; enum values are the lowercase API values (live, admin), not the DB names.
"""
import datetime
import decimal
import hmac
import math
import os
import re
import uuid

from flask import Blueprint, current_app, jsonify, request

from . import ai_usage, metrics_sync, value_dashboard as vd
from .extensions import db
from .models import (
    Automation,
    Department,
    MetricsSnapshot,
    PendingAutomation,
    Skill,
    Status,
    Subscription,
    User,
)

API_VERSION = "1.0"
TOKEN_ENV = "INTEGRATION_API_TOKEN"
MIN_TOKEN_LENGTH = 32
# The org sync runs daily at 03:00 UTC (github-org-sync-cron) and stores a metrics snapshot for every repo that has
# dashboard/metrics.json: a newest snapshot older than 36 h means a missed run.
STALE_AFTER_HOURS = 36
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")

bp = Blueprint("integration_api", __name__, url_prefix="/api/integration/v1/automation")


# --- plumbing ------------------------------------------------------------------------------------------------

class _Refused(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error(status, code, message):
    return jsonify({"error": {"code": code, "message": message}}), status


@bp.before_request
def _authorize():
    expected = (os.environ.get(TOKEN_ENV) or "").strip()
    if len(expected) < MIN_TOKEN_LENGTH:
        return _error(503, "integration_disabled", "integration API is not configured")
    header = request.headers.get("Authorization") or ""
    given = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not given or not hmac.compare_digest(given.encode(), expected.encode()):
        return _error(401, "unauthorized", "invalid or missing bearer token")
    return None


@bp.errorhandler(_Refused)
def _refused(e):
    return _error(e.status, e.code, e.message)


@bp.after_request
def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def _iso(value):
    """date -> YYYY-MM-DD; datetime (naive = UTC, as stored) -> ISO-8601 with Z; anything else unchanged."""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return value.replace(microsecond=0).isoformat() + "Z"
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


def _num(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, decimal.Decimal):
        value = float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _plain(value):
    """JSON-safe copy of plain data (dicts / lists / scalars from the DB JSON columns and the dashboard helpers)."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return _iso(value)
    if isinstance(value, (decimal.Decimal, float, int)) and not isinstance(value, bool):
        return _num(value)
    return value


def _text_or_none(fn, value, **kw):
    return None if value is None else fn(value, **kw)


def _data_as_of():
    newest = MetricsSnapshot.query.with_entities(MetricsSnapshot.fetched_at).order_by(MetricsSnapshot.fetched_at.desc()).first()
    if newest and newest[0]:
        return newest[0]
    newest = Automation.query.with_entities(Automation.updated_at).order_by(Automation.updated_at.desc()).first()
    return newest[0] if newest and newest[0] else None


def _envelope(contract, payload):
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    as_of = _data_as_of()
    if as_of is not None and as_of.tzinfo is not None:
        as_of = as_of.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    status = "no_data" if as_of is None else ("stale" if now - as_of > datetime.timedelta(hours=STALE_AFTER_HOURS) else "fresh")
    rid = request.headers.get("X-Request-Id") or ""
    body = {
        "api_version": API_VERSION,
        "contract": contract,
        "request_id": rid if REQUEST_ID_RE.match(rid) else uuid.uuid4().hex,
        "generated_at": _iso(now),
        "data_as_of": _iso(as_of),
        "freshness": {"status": status, "stale_after_hours": STALE_AFTER_HOURS},
        "period": None,  # state-now resources: no reporting period
    }
    body.update(payload)
    return jsonify(body)


def _int_arg(name, minimum=1, maximum=10**9):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    if not raw.isdigit() or not (minimum <= int(raw) <= maximum):
        raise _Refused(400, "invalid_params", f"{name} must be an integer")
    return int(raw)


def _status_arg(allow_all):
    raw = request.args.get("status")
    values = {s.value for s in Status}
    if raw in (None, ""):
        return None
    if raw == "all" and allow_all:
        return "all"
    if raw not in values:
        raise _Refused(400, "invalid_params", "unknown status")
    return raw


def _today_arg():
    raw = request.args.get("today")
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        raise _Refused(400, "invalid_params", "today must be YYYY-MM-DD") from None


MAX_ONLY = 1000


def _only_arg():
    """`only` present -> the set of slugs the answer is restricted to (may be empty: nothing); absent -> None (all)."""
    if "only" not in request.args:
        return None
    raw = request.args.get("only") or ""
    slugs = [s for s in raw.split(",") if s]
    if len(slugs) > MAX_ONLY or not all(SLUG_RE.match(s) for s in slugs):
        raise _Refused(400, "invalid_params", "only must be a comma-separated list of slugs")
    return set(slugs)


def _restrict(query, only):
    if only is None:
        return query
    if not only:
        return query.filter(db.false())
    return query.filter(Automation.slug.in_(sorted(only)))


# --- shared shapes -------------------------------------------------------------------------------------------

def _status(status):
    return {"value": status.value, "label": status.label, "variant": status.bs_variant}


def _department(d):
    return {"id": d.id, "name": d.name, "hue": d.hue}


def _person(u):
    if u is None:
        return None
    return {"id": u.id, "name": u.name, "initials": u.initials, "role": u.role.value, "role_label": u.role.label}


def _security(obj):
    return {"state": obj.security_review_state, "label": obj.security_review_label, "at": _iso(obj.security_review_at),
            "high": obj.security_review_high, "medium": obj.security_review_medium}


def _automation_ref(a):
    return {"slug": a.slug, "name": a.name, "one_liner": a.one_liner, "status": _status(a.status),
            "departments": [_department(d) for d in sorted(a.departments, key=lambda d: d.name)], "owner": _person(a.owner)}


def _roi_brief(a):
    r = a.roi
    if r is None:
        return None
    best = _num(r.best_hours_saved_per_month)
    return {
        "confidence": r.confidence, "measured_value": r.measured_value,
        "best_hours_saved_per_month": best, "best_hours_text": _text_or_none(vd.fmt_hours, best),
        "measured_hours_per_month": _num(r.measured_hours_per_month),
        "dev_hours": _num(r.dev_hours), "dev_hours_text": _text_or_none(vd.fmt_number, r.dev_hours),
        "maintenance_hours_per_month": _num(r.maintenance_hours_per_month),
        "maintenance_text": _text_or_none(vd.fmt_number, r.maintenance_hours_per_month),
    }


# --- /value --------------------------------------------------------------------------------------------------

def _metric_entry(e):
    m = e["metric"]
    money = m.money if isinstance(m.money, dict) else None
    target = m.target if isinstance(m.target, dict) else None
    unit = m.unit or ""
    return {
        "key": m.metric_key, "name": m.name, "type": m.type, "type_label": e.get("type_label"), "unit": m.unit,
        "direction": m.direction, "is_primary": bool(m.is_primary), "method": m.method,
        "check_every_days": m.check_every_days, "target_why": target.get("why") if target else None,
        "money_config": _plain(money) if money else None,
        "status": e["status"], "label": e["label"], "verdict": e["verdict"], "overdue": e["overdue"],
        "next_due": _iso(e["next_due"]), "next_due_text": _text_or_none(vd.fmt_date, e["next_due"]),
        "baseline": _num(e["baseline"]), "baseline_text": _text_or_none(vd.fmt_number, e["baseline"]),
        "baseline_parts": _plain(e.get("baseline_parts")),
        "target_value": _plain(e.get("target_value")),
        "target_text": None if e.get("target_value") is None else vd.fmt_number(e["target_value"]),
        "latest_measured": _num(e["latest_measured"]), "latest_measured_text": _text_or_none(vd.fmt_number, e["latest_measured"]),
        "latest_measured_date": _iso(e["latest_measured_date"]), "latest_measured_date_text": _text_or_none(vd.fmt_date, e["latest_measured_date"]),
        "trend": e.get("trend"),
        "improvement": _num(e["improvement"]), "improvement_estimate": _num(e.get("improvement_estimate")),
        "money_measured": _num(e["money_measured"]), "money_estimate": _num(e["money_estimate"]),
        "money_measured_words": e.get("money_measured_words"), "money_estimate_words": e.get("money_estimate_words"),
        "confirmation": _plain(e.get("confirmation")),
        "confirmation_date_text": _text_or_none(vd.fmt_date, (e.get("confirmation") or {}).get("when")),
        "asked": _plain(e.get("asked") or {}),
        "history": [{
            "date": _iso(h.get("date")), "date_text": h.get("date_text"), "period": h.get("period"), "period_text": h.get("period_text"),
            "value": _plain(h.get("value")), "value_text": h.get("value_text"), "kind": h.get("kind"),
            "source_text": h.get("source_text"), "source_tech": h.get("source_tech"), "source_full": h.get("source") or "",
            "note": h.get("note"),
        } for h in e.get("history") or []],
        "unit_is_percent": unit.strip().startswith("%"),
    }


def _value_block(v):
    if not v:
        return None
    out = _plain({k: v[k] for k in ("totals", "seen", "net", "status", "status_label", "status_badge", "categories")})
    out["net_text"] = _text_or_none(vd.fmt_usd, v["net"])
    out["totals_text"] = {k: vd.fmt_usd(t) for k, t in v["totals"].items()}
    out["items"] = []
    for i in v["items"]:
        item = _plain({k: i[k] for k in ("key", "category", "category_label", "pill_kind", "pill_class", "title", "usd", "usd_text",
                                          "status", "label", "badge", "basis_label", "formula", "why", "source", "source_tech",
                                          "source_full", "as_of_text", "asked", "is_run_cost", "confirmation")})
        item["confirmation_date_text"] = _text_or_none(vd.fmt_date, (i.get("confirmation") or {}).get("when"))
        out["items"].append(item)
    return out


def _value_row(row):
    a = row["automation"]
    rejected = row["rejected"]
    return {
        "automation": _automation_ref(a),
        "format": row["format"],
        "launched_at": _iso(row["launched_at"]),
        "tile": row["tile"],
        "has_measured": row["has_measured"],
        "rejected": None if not rejected else {
            "errors": list(rejected.get("errors") or []), "human_errors": list(rejected.get("human_errors") or []),
            "when": _iso(rejected.get("when")), "when_text": _text_or_none(vd.fmt_date, rejected.get("when")),
            "sha": rejected.get("sha"), "hint": rejected.get("hint")},
        "snapshot_sha": row["snapshot_sha"],
        "metrics": [_metric_entry(e) for e in row["metrics"]],
        "primary_key": row["primary"]["metric"].metric_key if row.get("primary") else None,
        "value": _value_block(row["value"]),
        "at_scale": _plain(row["at_scale"]),
        "money_summary": _plain(row.get("money_summary")),
        "fresh": _plain(row.get("fresh")),
        "question_groups": _plain(row["question_groups"]),
        "open_questions_n": len(row["open_questions"]),
        "saved_hours": _plain(row["saved_hours"]),
        "events": [{"when": _iso(ev["when"]),
                    "when_text": ev["when"].strftime("%d.%m.%Y %H:%M") if isinstance(ev["when"], datetime.datetime) else None,
                    "who": ev["who"], "text": ev["text"]} for ev in row["events"]],
        "stage1_types": [{"key": k, "label": vd.METRIC_LABELS[k]} for k in vd.target_metrics(a)],
        "roi": _roi_brief(a),
    }


@bp.get("/value")
def value():
    status = _status_arg(allow_all=True) or Status.LIVE.value
    department = _int_arg("department")
    owner = _int_arg("owner")
    today = _today_arg()
    only = _only_arg()
    query = _restrict(Automation.query, only)
    if status != "all":
        query = query.filter(Automation.status == Status(status))
    if department:
        query = query.filter(Automation.departments.any(Department.id == department))
    if owner:
        query = query.filter(Automation.owner_id == owner)
    automations = query.order_by(Automation.name).all()
    ctx = vd.build_context(automations, today=today)

    every = _restrict(Automation.query, only).all()
    departments = sorted({d for a in every for d in a.departments}, key=lambda d: d.name)
    owners = sorted({a.owner for a in every if a.owner}, key=lambda u: u.name)
    subscriptions = Subscription.query.order_by(Subscription.name).all()
    if only is not None:
        subscriptions = [s for s in subscriptions if any(a.slug in only for a in s.automations)]
    totals = {k: _plain(ctx[k]) for k in ("total_n", "saved_total", "saved_measured", "saved_estimated", "tiles", "partial_n",
                                          "rejected_verdict_n", "old_format_n", "confidence_pct", "measured_auto_n", "has_money",
                                          "money_total", "money", "money_seen", "at_scale_total", "at_scale_n", "dev_total", "dev_n",
                                          "maint_total", "maint_n", "subs_cost_total", "subs_linked_count", "net_scale")}
    texts = {
        "saved_total": _text_or_none(vd.fmt_hours, ctx["saved_total"]),
        "saved_measured": vd.fmt_hours(ctx["saved_measured"]),
        "saved_estimated": vd.fmt_hours(ctx["saved_estimated"]),
        "money_total": vd.fmt_usd(ctx["money_total"]),
        "money": {k: vd.fmt_usd(v) for k, v in ctx["money"].items()},
        "at_scale_total": _text_or_none(vd.fmt_usd, ctx["at_scale_total"]),
        "subs_cost_total": vd.fmt_usd(ctx["subs_cost_total"]),
        "dev_total": _text_or_none(vd.fmt_hours, ctx["dev_total"]),
        "maint_total": _text_or_none(vd.fmt_hours, ctx["maint_total"]),
        "net_scale": "%.0f" % ctx["net_scale"],
    }
    type_keys = {v: k for k, v in vd.TYPE_LABELS.items()}
    return _envelope("automation.value", {
        "filters": {"status": status, "department": department, "owner": owner, "today": _iso(today) if today else None},
        "options": {
            "departments": [_department(d) for d in departments],
            "owners": [{"id": u.id, "name": u.name} for u in owners],
            "statuses": [{"value": s.value, "label": "Запущені" if s == Status.LIVE else s.label} for s in Status] + [{"value": "all", "label": "Усі статуси"}],
        },
        "totals": totals,
        "texts": texts,
        "metric_counts": [{"type": type_keys.get(m["label"]), "label": m["label"], "count": m["count"], "pct": m["pct"]} for m in ctx["metric_counts"]],
        "net_rows": [{"slug": r["automation"].slug, "name": r["automation"].name, "net": _num(r["net"]),
                      "net_text": vd.fmt_hours(r["net"], signed=True), "measured": r["measured"]} for r in ctx["net_rows"]],
        "rows": [_value_row(r) for r in ctx["rows"]],
        "subscriptions": [{"id": s.id, "name": s.name, "provider": s.provider, "monthly_cost_usd": _num(s.monthly_cost_usd),
                           "monthly_cost_text": vd.fmt_usd(s.monthly_cost_usd), "automations_n": len(s.automations)} for s in subscriptions],
    })


# --- /automations (registry) ---------------------------------------------------------------------------------

@bp.get("/automations")
def automations():
    status = _status_arg(allow_all=False)
    department = _int_arg("department")
    q = (request.args.get("q") or "").strip()[:200]
    only = _only_arg()
    query = _restrict(Automation.query, only)
    if status:
        query = query.filter(Automation.status == Status(status))
    if department:
        query = query.filter(Automation.departments.any(Department.id == department))
    if q:
        query = query.filter(Automation.name.ilike(f"%{q}%"))
    rows = query.order_by(Automation.updated_at.desc()).all()
    every = _restrict(Automation.query, only).all()
    counts = {s.value: sum(1 for a in every if a.status == s) for s in Status}

    def roi_text(a):
        if a.roi is None:
            return None
        return a.roi.measured_value or "оцінка"

    return _envelope("automation.registry", {
        "filters": {"status": status, "department": department, "q": q or None},
        "counts": {"total": len(every), "by_status": counts, "needs_review": sum(1 for a in every if a.security_review_at is None)},
        "statuses": [{"value": s.value, "label": s.label, "count": counts[s.value]} for s in Status],
        "departments": [_department(d) for d in (Department.query.order_by(Department.name).all() if only is None
                                                 else sorted({d for a in every for d in a.departments}, key=lambda d: d.name))],
        "rows": [dict(_automation_ref(a), security=_security(a), roi_text=roi_text(a), updated_at=_iso(a.updated_at)) for a in rows],
    })


@bp.get("/pending")
def pending():
    rows = PendingAutomation.query.filter_by(dismissed=False).order_by(PendingAutomation.discovered_at.desc()).all()
    return _envelope("automation.pending", {
        "rows": [{"slug": p.slug, "name": p.name, "repo_url": vd_safe_url(p.repo_url), "missing": p.missing,
                  "discovered_at": _iso(p.discovered_at), "last_seen_at": _iso(p.last_seen_at)} for p in rows],
    })


def vd_safe_url(value):
    value = (value or "").strip()
    return value if value.lower().startswith(("http://", "https://")) else None


# --- /automation?slug= (detail page) -------------------------------------------------------------------------

@bp.get("/automation")
def automation_detail():
    slug = (request.args.get("slug") or "").strip()
    if not SLUG_RE.match(slug):
        raise _Refused(400, "invalid_params", "slug is required")
    a = Automation.query.filter_by(slug=slug).first()
    if a is None:
        raise _Refused(404, "not_found", "no such automation")
    reflow = current_app.jinja_env.filters["reflow"]
    roi = a.roi
    subs = [{"name": s.name, "cost_per_automation": _num(s.cost_per_automation), "shared_by": len(s.automations)}
            for s in sorted(a.subscriptions, key=lambda s: s.name)]
    usage = None
    if a.ai_usage_project_id:
        summary = ai_usage.get_usage_summary(a)
        usage = {"project_id": a.ai_usage_project_id, "available": summary is not None}
        if summary is not None:
            usage.update({k: _plain(summary.get(k)) for k in ("month_spend_usd", "budget_usd", "budget_pct", "trailing_7d_avg_usd",
                                                               "today_spend_usd", "has_any_data")})
    todo = list(a.todo_items)
    comparison = a.comparison
    return _envelope("automation.detail", {
        "automation": dict(_automation_ref(a), description=reflow(a.description) or None,
                           repo_url=vd_safe_url(a.repo_url), clickup_url=vd_safe_url(a.clickup_url),
                           stage1_phases=len(a.stage1_answers or {}), security=_security(a),
                           created_at=_iso(a.created_at), updated_at=_iso(a.updated_at)),
        "pages": [{"name": p.name, "description": reflow(p.description) or None, "detail": reflow(p.detail) or None} for p in a.pages],
        "skills": [{"id": s.id, "name": s.name} for s in sorted(a.skills, key=lambda s: s.name)],
        "connections": [{"slug": c.connected_automation.slug, "name": c.connected_automation.name,
                         "status": _status(c.connected_automation.status),
                         "department": _department(c.connected_automation.departments[0]) if c.connected_automation.departments else None}
                        for c in a.connections if c.connected_automation is not None],
        "todo": {"done": sum(1 for t in todo if t.done), "total": len(todo), "pending": [t.text for t in todo if not t.done]},
        "review_log": [{"round_label": r.round_label, "found": r.found, "changed": r.changed, "rejected": r.rejected} for r in a.review_log[:5]],
        "comparison": None if comparison is None else {
            "old_way_description": comparison.old_way_description, "limitations": comparison.limitations,
            "features": [{"feature": f.feature, "old_way": f.old_way, "new_way": f.new_way, "why_it_matters": f.why_it_matters} for f in comparison.features]},
        "roi": None if roi is None else {
            "confidence": roi.confidence, "measured_value": roi.measured_value, "metric_description": roi.metric_description,
            "hypothesis": roi.hypothesis, "qualitative_notes": [x for x in (roi.qualitative_notes or "").split("\n") if x.strip()],
            "presentation_url": vd_safe_url(roi.presentation_url)},
        "costs": {
            "dev_hours": _num(roi.dev_hours) if roi else None, "dev_hours_text": _text_or_none(vd.fmt_number, roi.dev_hours) if roi else None,
            "maintenance_hours_per_month": _num(roi.maintenance_hours_per_month) if roi else None,
            "maintenance_text": _text_or_none(vd.fmt_number, roi.maintenance_hours_per_month) if roi else None,
            "net_hours_per_month": _num(roi.net_hours_per_month) if roi else None,
            "subscriptions": subs,
            "subscriptions_total": _num(a.monthly_subscription_cost_usd),
        },
        "tokens": usage,
    })


# --- people, skills, departments, subscriptions ---------------------------------------------------------------

@bp.get("/automator")
def automator():
    user_id = _int_arg("id")
    if user_id is None:
        raise _Refused(400, "invalid_params", "id is required")
    u = db.session.get(User, user_id)
    if u is None:
        raise _Refused(404, "not_found", "no such user")
    owned = sorted(u.automations, key=lambda a: a.name)
    return _envelope("automation.automator", {
        "person": _person(u),  # never the e-mail, the api_key or the GitHub login
        "automations": [_automation_ref(a) for a in owned],
    })


@bp.get("/people")
def people():
    """Everyone with an Automation System account: who they are and what they own. The ONLY answer with an e-mail
    (the work e-mail): Supplax links people to its own users with it, on the server, admins only."""
    users = User.query.order_by(User.name).all()
    return _envelope("automation.people", {
        "rows": [dict(_person(u), email=(u.email or "").strip().lower() or None, approved=bool(u.is_approved),
                      automations=sorted(a.slug for a in u.automations)) for u in users],
    })


@bp.get("/skills")
def skills():
    rows = Skill.query.order_by(Skill.name).all()
    return _envelope("automation.skills", {
        "rows": [{"id": s.id, "name": s.name, "description": s.description, "doc_url": vd_safe_url(s.doc_url),
                  "repo_url": vd_safe_url(s.repo_url), "security": _security(s),
                  "automations": [{"slug": a.slug, "name": a.name} for a in sorted(s.automations, key=lambda a: a.name)]} for s in rows],
    })


@bp.get("/departments")
def departments():
    rows = Department.query.order_by(Department.name).all()
    return _envelope("automation.departments", {
        "rows": [dict(_department(d), automations_n=len(d.automations)) for d in rows],
    })


@bp.get("/subscriptions")
def subscriptions():
    rows = Subscription.query.order_by(Subscription.name).all()
    total = sum((s.monthly_cost_usd for s in rows), start=0)
    return _envelope("automation.subscriptions", {
        "total_monthly_usd": _num(total) if rows else 0.0,
        "rows": [{"id": s.id, "name": s.name, "provider": s.provider, "monthly_cost_usd": _num(s.monthly_cost_usd),
                  "notes": s.notes, "automations_n": len(s.automations)} for s in rows],
    })


def register_integration_api(app, csrf):
    """Wire the blueprint: CSRF does not apply (GET only, bearer token, no session)."""
    csrf.exempt(bp)
    app.register_blueprint(bp)
