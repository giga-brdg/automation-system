"""Aggregation for the portfolio-wide /value page (automation_value_dashboard
in src/app.py) - turns a list of Automation rows into the numbers the
template needs. Kept separate from the route so the arithmetic is
unit-testable without a request/response round trip, same reason
src/ai_usage.py and src/stage1_questions.py are their own modules.

Every total here only ever sums a single honest unit (hours saved/month,
hours spent/month, dollars/month) - never blended across units, matching
dashboard/ROI.md's own rule that an invented number is the one thing this
kind of figure must never contain. dev_hours is deliberately excluded from
every monthly total: it's a one-time cost, not a recurring one.
"""

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


def target_metrics(automation):
    """Token list from Stage 1 Phase 1's target_metric_type answer (see
    src/stage1_questions.py) - [] for an automation that hasn't answered
    Stage 1 yet, or whose stage1_answers predates this field."""
    phase1 = (automation.stage1_answers or {}).get("1", {})
    tokens = phase1.get("target_metric_type") or []
    return [t for t in tokens if t in METRIC_LABELS]


def build_context(automations):
    with_saved = [a for a in automations if a.roi and a.roi.best_hours_saved_per_month is not None]
    saved_total = sum((a.roi.best_hours_saved_per_month for a in with_saved), start=0)
    saved_measured = sum(
        (a.roi.best_hours_saved_per_month for a in with_saved if a.roi.measured_hours_per_month is not None),
        start=0,
    )
    saved_estimated = saved_total - saved_measured

    measured_n = sum(1 for a in automations if a.roi and a.roi.confidence == "measured")
    estimated_n = sum(1 for a in automations if a.roi and a.roi.confidence != "measured")
    none_n = sum(1 for a in automations if not a.roi)
    with_roi_n = measured_n + estimated_n
    confidence_pct = round(measured_n / with_roi_n * 100) if with_roi_n else None

    devs = [a.roi.dev_hours for a in automations if a.roi and a.roi.dev_hours is not None]
    maints = [a.roi.maintenance_hours_per_month for a in automations
              if a.roi and a.roi.maintenance_hours_per_month is not None]
    with_subs = [a for a in automations if a.monthly_subscription_cost_usd is not None]
    subs_cost_total = sum((a.monthly_subscription_cost_usd for a in with_subs), start=0)
    linked_sub_ids = {s.id for a in automations for s in a.subscriptions}

    counts = {k: 0 for k in METRIC_ORDER}
    max_count = 0
    for a in automations:
        for tok in target_metrics(a):
            counts[tok] += 1
            max_count = max(max_count, counts[tok])
    metric_counts = [
        {"label": METRIC_LABELS[k], "css_var": f"--metric-{k.split('_')[0]}", "count": counts[k],
         "pct": round(counts[k] / max_count * 100) if max_count else 0}
        for k in METRIC_ORDER
    ]

    net_rows = []
    for a in automations:
        if a.roi and a.roi.net_hours_per_month is not None:
            net_rows.append({
                "automation": a,
                "net": float(a.roi.net_hours_per_month),
                "measured": a.roi.measured_hours_per_month is not None,
            })
    net_rows.sort(key=lambda r: r["net"], reverse=True)
    net_scale = max([10.0] + [abs(r["net"]) for r in net_rows])

    return {
        "saved_total": float(saved_total) if with_saved else None,
        "saved_measured": float(saved_measured),
        "saved_estimated": float(saved_estimated),
        "total_n": len(automations),
        "measured_n": measured_n,
        "estimated_n": estimated_n,
        "none_n": none_n,
        "confidence_pct": confidence_pct,
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
