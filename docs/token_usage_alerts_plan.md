# Token Usage / Budget Alerts — Implementation Plan

The detailed companion to the token-usage panel on an automation's detail
page and the `check-token-usage` CLI command. Reuses `telegram.send_message`
and the shared-`ADMIN_TELEGRAM_CHAT_ID`-channel decision already made in
`docs/telegram_alerts_plan.md` — see that doc for the delivery mechanism
itself; this one doesn't re-litigate that choice, it's a different alert
*source* (a periodic pull against another system's data, not a push from an
automation reporting its own failure).

## Blocking prerequisite — read this first

Supplax already runs a separate Railway service, **ai-usage-collector**
(project "AI Usage Registry", repo `giga-brdg/ai-usage-collector`), which
pulls OpenAI usage/cost data per API key into its own Postgres. Its schema
already models exactly what this feature needs — `projects` ↔
`ai_credentials` (one OpenAI key per project) ↔ `ai_usage_daily`/
`ai_cost_daily` (per-day cost/tokens per project) — but as of this writing:

- `ai_usage_daily`, `ai_cost_daily`, and `sync_runs` are all **empty (0
  rows)**. The service has been live since 2026-09-01 and successfully
  talked to OpenAI's API once (a "discovery" pass on 2026-09-02), but has
  never completed a real sync that persists data.
- Only **2 projects** are registered there ("Quality", "Sales Trainer") —
  neither matches this dashboard's real automations (stage-0-forecast,
  hr-vacancy-bot, automation-system itself).

Everything in this plan works correctly against that state — it renders "no
data yet" everywhere and `check-token-usage` sends zero alerts — but it
cannot produce a single real number or real alert until someone (that
system's owner; recent committer: Stanislav, supplax.com.ua) makes
ai-usage-collector actually run its sync and registers each real
automation's OpenAI key there as a `projects` + `ai_credentials` row. That's
a dependency on a different, private repo — not something phases 1-3 below
can complete on their own.

## Phase 1 — Schema + read-only data access (done)

- `Automation.ai_usage_project_id` (manual link to ai-usage-collector's
  `projects.id` — no cross-DB join is possible, and with only 2 unrelated
  rows there today, a live dropdown would add complexity for no benefit; a
  plain manual int is the v1, a dropdown fed by a live read is a reasonable
  v2 once more automations are actually linked), `monthly_token_budget_usd`,
  `token_spike_multiplier`, `last_token_alert_kind`/`_at` (de-dup state) —
  added via the `migrate-token-usage` CLI command (same idempotent
  `ALTER TABLE` pattern as `migrate-skill-repo-url`).
- `src/ai_usage.py` — first second-database connection in this codebase.
  Raw `psycopg2` (already a dependency via SQLAlchemy, no new package),
  read-only session, plain SQL against `ai_usage_daily` (no ORM models —
  these are another system's tables). Returns `None` when not linked or the
  DB is unreachable; returns `has_any_data=False` distinctly from a real
  `$0.00` when the project is linked but has zero usage rows — today's real
  state for every project.

## Phase 2 — UI

- `automation_form.html` — three optional fields: ai-usage-collector project
  ID, monthly budget ($), spike multiplier (defaults to
  `ai_usage.SPIKE_MULTIPLIER_DEFAULT` = 3.0x if left blank).
- `automation_detail.html`'s sidebar — a "Токени" panel (same `panel`/
  `panel-title` shape as the existing ROI/Статус panels), shown only when
  `ai_usage_project_id` is set, with three distinct states: DB unreachable,
  linked-but-no-data-yet, and real numbers (month spend vs. budget, today vs.
  7-day average).

## Phase 3 — Alerting

- `check-token-usage` Flask CLI command: iterates automations with a linked
  project, evaluates budget (≥80% warn, ≥100% critical) and spike (today's
  spend > N× trailing-7-day average) thresholds in that priority order,
  sends one Telegram message per new threshold-cross via the existing
  `telegram.send_message` helper, and records `last_token_alert_kind`/`_at`
  so the same cross doesn't re-alert every run (the flag clears once spend
  drops back under every threshold, so a later re-cross alerts again).
- **Must run via Railway's Cron Schedule on its own service, not
  continuously** — confirmed directly from Railway's docs: Cron Schedule is
  a per-service Settings field that runs that service's Start Command on a
  crontab expression and requires the process to exit when done (a
  still-`Active` previous run causes the next scheduled run to be *skipped*,
  not queued; minimum interval 5 minutes, UTC). This is exactly why
  `check-token-usage` is a single pass-and-exit CLI command, not folded into
  `telegram_bot.py`'s existing long-lived polling loop — that process's own
  continuous uptime in prod is already unconfirmed (see `DEPLOYMENT.md`),
  and stacking a second responsibility onto an already-uncertain process
  would double that risk instead of avoiding it.

### Manual setup (outside this repo's code)

1. Open a **permanent** TCP proxy on ai-usage-collector's Postgres (project
   `e62bd6cb-2995-4cb3-a00d-2a6cc15a0e80`, service
   `68543785-6c95-4403-9b3c-da2433467fa6`, environment
   `e6ffb091-a102-449c-ba45-52c569d979ce`) to get the connection string for
   `AI_USAGE_DATABASE_URL`. Ask ai-usage-collector's owner for a read-only
   Postgres role for this connection if they can provision one — this repo's
   code can't enforce read-only beyond the session-level flag `ai_usage.py`
   already sets.
2. In automation-system's own Railway project, create a new service (e.g.
   `token-usage-cron`) from the same repo (`giga-brdg/automation-system`,
   same convention `web`/`bot` already use), Start Command
   `flask --app src.app check-token-usage`, Cron Schedule e.g. `0 */3 * * *`
   (every 3 hours — usage data is daily-granularity, so sub-hourly checking
   buys nothing). Give it `DATABASE_URL`, `AI_USAGE_DATABASE_URL`,
   `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_CHAT_ID` (as shared/reference
   variables, not duplicated literal values).
3. Set `AI_USAGE_DATABASE_URL` on the `web` service too, for the detail-page
   panel.

## Open questions

- Whether a 3-hour cron cadence is right once real data exists, or a
  same-day spike needs tighter checking.
- Whether a live cross-DB dropdown for `ai_usage_project_id` (v2) is worth
  building once more than a couple of automations are actually linked.
- Whether the 80%/100% budget thresholds and 3x spike multiplier defaults
  need tuning — impossible to validate against real spend patterns today,
  since none exist yet.
