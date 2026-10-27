# Deployment

## Environments
Prod is live today, deployed to Railway (see commit `eedbdfa`'s "broke the Railway
build"). Three independent sources agree on this, though not in identical wording:
`dashboard/SUMMARY.md` has a `## Status` heading with value `live`, `SECURITY.md`
states "this is live, not a future plan," and `backlog/BACKLOG.md`'s 2026-09-01
entry (in Ukrainian) records the status as changed to `"Працює"` ("Working"/live).
Staging does not exist yet — `infra/staging/` and
`infra/prod/` are still empty placeholders (`infra/README.md`: no IaC tool has been
chosen). The staging+prod split named in `PIPELINE.md` §7 was a bootstrap-time
judgment call, never confirmed by the user and never actually built — treat
"staging" as aspirational, not a real environment, until it exists.

## Frontend assets
The UI is Bootstrap 5 / Able Pro (Phoenixcoded), vendored into `src/static/
vendor/able-pro/` from `github.com/giga-brdg/Design-system` @
`05ddaf1e70a3f832da268c7b28bc6c212dfb7f54`. Able Pro is commercially licensed,
internal Supplax use only. Don't make this repo's static assets public or
redistribute them. Brand palette and small first-party helpers live in
`src/static/supplax-overrides.css`. See `src/static/vendor/able-pro/VENDOR.md`
for the exact file list and how to re-vendor/re-retint after pulling a newer
commit of the design-system repo — it's not a plain asset copy, since
`css/style.css`/`css/style-preset.css` are recompiled from that repo's own
Sass with this app's brand colors.

## Deploy Steps
Currently deployed to Railway (PaaS). This is the practice as it actually happened,
reconstructed from the repo, not a pipeline anyone documented before building it —
the only Railway config in this repo is `railway.json`'s healthcheck (see "Before
deploy: dependencies"), so the real start command lives in Railway's project
settings, not in git:
- **Stack**: Python/Flask (`Flask`, `Flask-SQLAlchemy`, `Flask-Login` — not the
  Node/Next.js stack `PIPELINE.md` Phase 2 records; that phase is stale, the
  shipped code is Flask). Entry point is `src/app.py`'s module-level `app =
  create_app()`. The start command lives in Railway's service settings, not in
  git; checked 2026-10-27 it is `gunicorn -w 2 -b 0.0.0.0:$PORT src.app:app`,
  with no pre-deploy command (so nothing runs migrations on deploy). No Python version
  is pinned anywhere either (no `runtime.txt`, `.python-version`, or Railway/
  Nixpacks config), so a from-scratch deploy gets whatever Python version
  Railway's build system currently defaults to, which may not match what this
  was developed against.
- **Database**: `DATABASE_URL` selects Postgres (`psycopg2-binary` is pinned for
  this); unset, it falls back to a local SQLite file (`data/portfolio.db`) — see
  `.env.example`, which frames SQLite as the default and Postgres as something to
  switch to "only if concurrent writes become a real problem." **Confirmed**
  (checked directly against the live Railway project, not inferred): prod runs a
  dedicated `Postgres` service, and the `web` service's `DATABASE_URL` points at
  it over Railway's private network — not the SQLite fallback. This resolves
  what used to be this section's biggest open risk: `data/portfolio.db` being
  git-ignored and living inside the app's own (ephemeral) container would have
  meant every redeploy could silently wipe all users, automations, and ROI data;
  that doesn't apply here, since prod was never on that fallback. Instance
  count is also confirmed low-risk: the `web` service runs a single active
  deployment/replica, so even a hypothetical SQLite scenario wouldn't have had
  the second failure mode (concurrent writers to divergent local files). There is
  also no migration framework (no Alembic/Flask-Migrate): schema is created/
  updated by hand via custom Flask CLI commands in `src/app.py` (`flask init-db`
  for a fresh DB; one `flask migrate-*` command per later schema change, each
  safe to re-run, see `register_cli` in `src/app.py`). **Prod state, checked
  2026-10-27** by comparing the live Postgres schema with `models.py` over a
  read-only connection: every `migrate-*` up to and including
  `migrate-github-username` has been applied (registration, confirm-attempts,
  skill-repo-url, token-usage, stage0-answers, rename-stage0-to-stage1,
  roi-time-metrics, cost-metrics, security-review, pending-automations,
  github-username). `automation` also still carries four columns the model no
  longer has (`current_stage_override`, `last_commit_at`, `last_commit_message`,
  `last_synced_at`); they are nullable leftovers and harmless.
  `flask migrate-metrics` creates the five new metrics tables (`metrics_snapshot`,
  `automation_metric`, `metric_measurement`, `metric_confirmation`,
  `automation_event`) and touches nothing existing. Run it before deploying the
  code that uses them: until it runs, saving the card or Stage 1 form and syncing a
  repo that has `dashboard/metrics.json` fail, because each writes to
  `automation_event`. Viewing pages and login don't touch these tables.
  Catch for the first rollout: the running (old) code doesn't have the
  `migrate-metrics` command yet, so before that push the same tables are
  created by SQL compiled from the same models (`CreateTable` for the
  `postgresql` dialect), run in one transaction over `railway ssh --service web`.
  Once the code is live, `flask --app src.app migrate-metrics` should answer
  "Already migrated".
  `flask migrate-value-items` (contract v1.1) creates two more tables,
  `automation_value_item` and `value_item_confirmation`, and touches nothing
  existing. **Run it on prod before pushing the code that uses them**: that code
  reads `automation_value_item` on every `/value` load and on every sync of a
  repo with `dashboard/metrics.json`, so both fail until the tables exist. Same
  catch as `migrate-metrics`: the running code doesn't have the command yet, so
  for this first rollout create the two tables with SQL compiled from the same
  models (`CreateTable(AutomationValueItem.__table__)` and
  `CreateTable(ValueItemConfirmation.__table__)` for the `postgresql` dialect),
  in one transaction over `railway ssh --service web`, after a fresh backup (see
  Rollback). Once the code is live, `flask --app src.app migrate-value-items`
  should answer "Already migrated".
  A from-scratch or restored DB also has no login path until someone
  runs `flask --app src.app create-user <email> <name> --admin` (`src/app.py`'s
  `create-user` command, per `README.md`) to create the first admin — without
  this step `init-db`/`migrate-registration` alone leave nobody able to log in
  or approve further registrations. Every migration command above has to be run against
  the prod DB manually to have effect there — that's this doc's own
  characterization of what "run against prod" means, not a phrase used
  anywhere else in the repo (`README.md` documents all three purely as
  local-dev setup steps and never frames them as prod commands at all). The
  working path is SSH into the running `web` container, which already has the
  app and the private-network `DATABASE_URL`:
  `railway ssh --service web -- flask --app src.app migrate-<name>` (a local
  `railway run` can't reach the DB, its host is on Railway's private network).
  `create-user` specifically prompts interactively
  for a hidden, confirmed password (`click.prompt(..., hide_input=True,
  confirmation_prompt=True)`), so run it from an interactive `railway ssh
  --service web` session, not as a one-shot command. One more thing this step produces that whoever runs it needs to
  handle deliberately: `create-user`'s success output is
  `click.echo(f"Created ... (api_key: {user.api_key})")` — it prints the new
  admin's `api_key` in plain text. `README.md` already flags this key as
  printed exactly once with no UI to view or regenerate it, and `SECURITY.md`
  treats any `api_key` as a live credential regardless of source — so running
  this against prod means capturing that terminal output somewhere safe
  before it scrolls away, not just noting that a login now exists.
- **Env vars** (see `.env.example`): none of them make the app fail to start if
  unset — every read in `src/app.py`/`src/github_sync.py`/`src/telegram.py` goes
  through `os.environ.get(...)` with a fallback, not a hard requirement — but they
  aren't equally safe to skip in prod:
  - `ADMIN_TELEGRAM_CHAT_ID` is load-bearing, but not silently: if it's unset,
    `telegram_bot.py`'s `run()` calls `raise SystemExit(...)` and refuses to
    start at all — a loud crash of the approval-bot process, not a quiet
    degradation. And if the bot process *is* up but message delivery still
    fails, `/register` in `src/app.py` flashes a visible warning to the
    registrant ("не вдалося сповістити адміністратора..."), it doesn't swallow
    the failure either. What actually has no visible signal is narrower than
    "the flow silently can't complete": an admin who never notices the bot
    process died has no dashboard-side alert telling them registrations are
    piling up unapproved.
  - `AUTH_SECRET` and `TELEGRAM_BOT_TOKEN` fail *silently* rather than loudly if
    unset: `AUTH_SECRET` falls back to a hardcoded insecure dev key (`src/
    app.py`), and a missing `TELEGRAM_BOT_TOKEN` makes `src/telegram.py` return
    `None` from every call instead of erroring — both are real prod
    requirements even though the code won't tell you they're missing.
  - `DATABASE_URL` and `GITHUB_TOKEN` are genuinely optional: no `DATABASE_URL`
    just means SQLite instead of Postgres; no `GITHUB_TOKEN` just means
    unauthenticated (rate-limited) GitHub API calls in `github_sync.py`.
  - `CLICKUP_API_TOKEN` is listed in `.env.example` but is never read anywhere in
    `src/` (`grep -rn CLICKUP_API_TOKEN src/` matches nothing outside a comment)
    — it does nothing today; don't treat it as required.
- **`src/telegram_bot.py` runs as a separate long-lived process** (polling, not a
  webhook) that must be deployed and kept running alongside the web process —
  shipping only the web app leaves registration approval dead with no error shown
  to the person trying to register. **Confirmed** (checked directly against
  the live Railway project): it runs as its own dedicated Railway service
  (named `bot`), separate from `web`, currently in a healthy `SUCCESS`
  deployment state — not a worker process, not left un-deployed. There's
  still no `Procfile`/second-service config *in this repo* recording that,
  so this fact lives in Railway's project settings, not in git — worth
  writing down here precisely because git can't show it.
- **Confirmed project identity and URL**: this is the `Automation Dashboard`
  project (workspace `giga-brdg`), reachable at
  `web-production-c6a51.up.railway.app` — Railway's default subdomain, no
  custom domain attached. Previously nothing in this repo recorded either, so
  a newcomer had no way to even locate the deployment; both are now settled.
  The project also runs a `token-usage-cron` service (`check-token-usage`,
  see `docs/token_usage_alerts_plan.md`) and its own `Postgres` — both
  confirmed live alongside `web` and `bot`.
- **`sync-github-org` runs on its own cron service — `github-org-sync-cron`,
  confirmed live** alongside `web`/`bot`/`token-usage-cron`. Same treatment as
  `token-usage-cron`: `flask --app src.app sync-github-org giga-brdg`, daily at
  03:00 UTC (`0 3 * * *` — new repos and README/ROI edits aren't minute-to-minute
  events, unlike token spend). `DATABASE_URL` and `GITHUB_TOKEN` are Railway
  reference variables (`${{Postgres.DATABASE_URL}}` / `${{web.GITHUB_TOKEN}}`), not
  literal copies, so rotating either on its source service updates this one too.
  `AUTOMATION_SYNC_OWNER_EMAIL` is set directly on this service.
  The same scan also runs on demand via the "Оновити з GitHub" button on
  `/automations` (`automations_sync_github_org` in `src/app.py`) for whoever
  doesn't want to wait for the next 03:00 UTC run - it needs `GITHUB_SYNC_ORG`
  set on the `web` service too (same value, `giga-brdg`), which isn't one of
  the reference variables above since `web` doesn't otherwise need to know
  the org name.
- **Still genuinely undecided / undocumented**: a real IaC setup for
  `infra/staging`/`infra/prod`; which Python version Railway's build actually resolves to
  (`RAILPACK_PYTHON_VERSION` is set on the live service, but that's a Railway
  project setting, not something pinned in this repo's own files — a
  from-scratch deploy elsewhere still has nothing here to pin it). The deploy
  trigger is no longer open: `web`'s source is the GitHub repo
  `giga-brdg/automation-system`, branch `main`, and every push to `main` gets
  its own deployment (checked 2026-10-27 against the deployment history), so
  a push is a deploy. (One thing that's *not* undecided, just unbuilt: there is no CI
  step ahead of a deploy, confirmed by `PIPELINE.md` §7 — a real test command
  now exists (`pytest`, see `TESTING.md`), but nothing runs it automatically,
  so a deploy today is still not blocked on tests passing. That's a settled
  fact about the current state, not an open question.)

## Rollout Strategy
Direct deploy: a push to `main` triggers it (see Deploy Steps), and there's no
canary/blue-green step in between. For the web app, it's just one new Railway
deployment replacing the running one.
That framing covers only the web process, though: Deploy Steps above can't
confirm whether `telegram_bot.py` runs as its own second Railway service, a
worker process, or isn't running at all, so if it does run as a separate
service, deploying it is a second, independent deployment event this
single-deployment description doesn't account for. Choosing anything more gradual
isn't meaningful yet since there's no real staging environment to graduate a
rollout through. Change history is *intended* to be tracked via `CHANGELOG.md` +
Conventional Commits, but per `PIPELINE.md` §7 this isn't actually happening yet
— `git tag` returns nothing and `CHANGELOG.md`'s `[Unreleased]` section still
only reads "Initial scaffold," unchanged since the 2026-08-23 initial commit,
despite ~20 real commits since. So a rollback does **not** have the
changelog-based record this might imply. What it does have is plain git history
— but even that is weaker than it sounds: per `git log --oneline`, only 4 of the
22 commits actually carry a `feat:`/`fix:`/`docs:` type prefix (confirmed by
direct count), and neither the most recent commit (`07454a2`) nor the merge
commit (`1b71353`) follow it. A reader scanning `git log --oneline` for the
offending change by its prefix will find 18 of 22 messages don't have one to
scan for — the log is still readable (message text plus timestamps), just not
the tidy prefix-tagged trail this might otherwise imply.

## Rollback
Git revert + redeploy — for application code only. It does **not** cover the
database: schema changes are applied by hand via the `flask migrate-registration`-
style CLI commands above, not through a reversible migration tool, so reverting the
commit that introduced a schema change does not undo it in the running database
(Postgres or SQLite, whichever prod actually is — see Deploy Steps). A rollback
that involves a schema change needs a manual, matching
down-step. The `Postgres` service's volume has **no backup schedule** on Railway,
so take one by hand before any schema change: a logical dump (`pg_dump`,
available inside the `Postgres` service's container) saved off Railway, and/or a
volume backup from the service's Backups tab. The last one, taken right before
`migrate-metrics` on 2026-10-27, exists in both forms: volume backup
`pre-metrics-2026-10-27` on Railway and `pg_dump -Fc` at
`D:\backups\automation-dashboard\pre-metrics-2026-10-27.dump` on the
maintainer's machine.

It also does not cover Railway project/service settings (start command, env
vars set in the dashboard, etc.) — as noted in Deploy Steps, those live outside
git entirely. If a bad deploy was actually caused by a settings change rather
than a code change, `git revert` has nothing to revert; whoever changed the
setting has to change it back by hand.

## Before deploy: dependencies
On 2026-10-27 a deploy went down on boot with `ModuleNotFoundError: psycopg`:
SQLAlchemy came in unpinned through Flask-SQLAlchemy, the fresh build resolved
2.1, which maps `postgresql://` to psycopg v3 (only `psycopg2-binary` is
installed), and with no healthcheck Railway marked the crashing deployment
SUCCESS and retired the working one. `requirements.txt` now pins SQLAlchemy,
Werkzeug and click, and `railway.json` sets `healthcheckPath: /login`, so a
deployment that can't serve `/login` within 100 s never takes traffic. When
`requirements.txt` changes, check it the way Railway builds it before pushing:
a fresh Python 3.12 venv, `pip install --no-cache-dir -r requirements.txt`,
then `create_engine('postgresql://u:x@localhost/db').dialect.dbapi` must import,
`create_app()` with `DATABASE_URL` pointing at an unreachable host must start,
and its first query must fail with `OperationalError` (not an import error),
and `pytest` must pass.

## Post-Deploy Verification
Railway's only automated check is the `/login` healthcheck above; there's no
monitoring beyond it (see `OBSERVABILITY.md`), so also verify a deploy manually:
confirm `/login` responds, confirm `telegram_bot.py` is running and an admin
`/grant <email> <role>` command still gets a response in Telegram, check the
app's Railway logs for startup errors, and — because both fail silently rather
than erroring (Deploy Steps above) — confirm `AUTH_SECRET` and
`TELEGRAM_BOT_TOKEN` are actually set to real values in the live Railway
environment, not just present locally in `.env`. If this deploy shipped a
schema change, also confirm the matching `flask migrate-registration`-style
command was actually run against the prod DB; nothing else in this doc's
process re-checks that it was.

**Caveat carried over from Deploy Steps, now partly resolved**: this used to say
none of this was as one-click as the list above sounds, since neither the
production URL nor the Railway project/service name were recorded anywhere.
Both now are (see Deploy Steps' "Confirmed project identity and URL" bullet):
`/login` is reachable at `web-production-c6a51.up.railway.app`, and the
project is `Automation Dashboard` under the `giga-brdg` workspace. What's
still not written down here is *access* to that workspace itself — reaching
its logs/settings/variables still means already having (or being granted)
Railway access to that project, which this doc can't grant on its own.
