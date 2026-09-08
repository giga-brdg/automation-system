"""Read-only access to ai-usage-collector's Postgres - a separate Railway
service's database, not this app's own (see AI_USAGE_DATABASE_URL). Plain SQL
via psycopg2, not SQLAlchemy models, since these are another system's tables
we don't own and must never migrate or write to from here.

ai-usage-collector tracks OpenAI usage/cost per its own `projects` row, one
row per automation that's registered there with its own OpenAI API key
(see `ai_credentials`). An Automation here links to that by
`Automation.ai_usage_project_id` (a plain manual int - the two systems'
projects don't share names or ids, so there's no automatic match).
"""
import os
from contextlib import contextmanager
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

SPIKE_MULTIPLIER_DEFAULT = 3.0
BUDGET_WARN_THRESHOLD = 0.8
BUDGET_CRITICAL_THRESHOLD = 1.0


@contextmanager
def _connect():
    """Yields a short-lived read-only connection, or None if
    AI_USAGE_DATABASE_URL isn't configured or the DB isn't reachable -
    callers must treat None as "no data available", never raise. Always
    closes the connection on exit, since this is used both by a
    request-handling web process and a one-shot CLI command that must not
    leave connections open."""
    url = os.environ.get("AI_USAGE_DATABASE_URL")
    if not url:
        yield None
        return
    conn = None
    try:
        conn = psycopg2.connect(url, connect_timeout=5)
        conn.set_session(readonly=True, autocommit=True)
        yield conn
    except psycopg2.OperationalError:
        yield None
    finally:
        if conn is not None:
            conn.close()


def _summarize(conn, project_id, budget_usd):
    """Cost comes from ai_cost_daily.amount_usd, not ai_usage_daily.cost_usd -
    confirmed against real synced data that ai_usage_daily's own cost_usd
    column is always NULL there (it only carries request/token counts per
    model); ai_cost_daily is where ai-usage-collector actually writes real
    dollar amounts per day, grouped by line item (e.g. "tts", "whisper").
    Summing across line items per day is correct here since we want the
    project's total spend, not a per-model breakdown."""
    today = date.today()
    month_start = today.replace(day=1)
    week_start = today - timedelta(days=7)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(amount_usd) FILTER (WHERE cost_date >= %(month_start)s), 0) AS month_spend_usd,
                COALESCE(SUM(amount_usd) FILTER (WHERE cost_date = %(today)s), 0) AS today_spend_usd,
                COALESCE(SUM(amount_usd) FILTER (
                    WHERE cost_date >= %(week_start)s AND cost_date < %(today)s
                ), 0) / 7.0 AS trailing_7d_avg_usd,
                COUNT(*) AS row_count
            FROM ai_cost_daily
            WHERE project_id = %(project_id)s
            """,
            {
                "month_start": month_start,
                "today": today,
                "week_start": week_start,
                "project_id": project_id,
            },
        )
        row = cur.fetchone()
    # Postgres returns these as Decimal; automation.monthly_token_budget_usd
    # may be a Decimal (SQLAlchemy Numeric via Postgres) or a plain
    # float/str (SQLite locally doesn't enforce the type). Decimal and float
    # can't be mixed in arithmetic, and both check-token-usage's threshold
    # math and the template's money formatting are simpler against plain
    # floats - normalize to float once here rather than at every call site.
    month_spend = float(row["month_spend_usd"])
    today_spend = float(row["today_spend_usd"])
    trailing_avg = float(row["trailing_7d_avg_usd"])
    budget = float(budget_usd) if budget_usd else None
    return {
        "month_spend_usd": month_spend,
        "budget_usd": budget,
        "budget_pct": (month_spend / budget) if budget else None,
        "trailing_7d_avg_usd": trailing_avg,
        "today_spend_usd": today_spend,
        "has_any_data": row["row_count"] > 0,
    }


def get_usage_summary(automation):
    """For the automation detail page. Returns None if not linked, or if
    AI_USAGE_DATABASE_URL is unset/unreachable - the template must render a
    distinct message for None vs. a real summary with has_any_data=False
    (linked, but ai_usage_daily has zero rows for it - today's real state
    for every automation, since ai-usage-collector has never completed a
    sync yet)."""
    if not automation.ai_usage_project_id:
        return None
    with _connect() as conn:
        if conn is None:
            return None
        return _summarize(conn, automation.ai_usage_project_id, automation.monthly_token_budget_usd)


def get_budget_and_spike_status(automation):
    """Same shape as get_usage_summary, for check-token-usage's threshold
    comparisons. Returns None when not linked or the DB isn't reachable -
    the caller skips the automation silently either way."""
    return get_usage_summary(automation)
