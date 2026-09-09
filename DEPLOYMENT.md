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

## Deploy Steps
Currently deployed to Railway (PaaS). This is the practice as it actually happened,
reconstructed from the repo, not a pipeline anyone documented before building it —
there is no Railway config file or `Procfile` in this repo, so the real start
command lives in Railway's project settings, not in git:
- **Stack**: Python/Flask (`Flask`, `Flask-SQLAlchemy`, `Flask-Login` — not the
  Node/Next.js stack `PIPELINE.md` Phase 2 records; that phase is stale, the
  shipped code is Flask). Entry point is `src/app.py`'s module-level `app =
  create_app()`. `gunicorn` is pinned in `requirements.txt`, which is consistent
  with it being the WSGI server, but since (as just noted) the real start command
  lives in Railway's project settings and not in git, this repo cannot actually
  confirm gunicorn is what Railway invokes — a pinned dependency proves it's
  installed, not that it's on the command line running in prod. No Python version
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
  for a fresh DB; `flask migrate-registration`, `flask migrate-skill-repo-url`,
  `flask migrate-token-usage`, and `flask migrate-confirm-attempts` for
  feature-specific columns added since — the last of these, for `/confirm`'s
  brute-force lockout, has **not yet been run against the production
  database** as of this writing, see `SECURITY.md`'s Known Limitations for
  what breaks until it is). A from-scratch or restored DB also has no login path until someone
  runs `flask --app src.app create-user <email> <name> --admin` (`src/app.py`'s
  `create-user` command, per `README.md`) to create the first admin — without
  this step `init-db`/`migrate-registration` alone leave nobody able to log in
  or approve further registrations. All three commands have to be run against
  the prod DB manually to have effect there — that's this doc's own
  characterization of what "run against prod" means, not a phrase used
  anywhere else in the repo (`README.md` documents all three purely as
  local-dev setup steps and never frames them as prod commands at all) — and
  this repo does not say *how* to actually reach the prod DB to run them;
  there's no
  documented `railway run`/shell/SSH mechanism for executing a one-off command
  against the deployed service. `create-user` specifically prompts interactively
  for a hidden, confirmed password (`click.prompt(..., hide_input=True,
  confirmation_prompt=True)`), which needs a real TTY attached to the remote
  process — a plain `railway run flask ... create-user ...` from a local
  terminal is the likely path, but that's an assumption, not something this repo
  documents. One more thing this step produces that whoever runs it needs to
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
- **Still genuinely undecided / undocumented**: a real IaC setup for
  `infra/staging`/`infra/prod`; the actual deploy trigger (push-to-`main`
  auto-deploy via Railway's GitHub integration vs. `railway up` vs. a manual
  dashboard click — nothing in this repo says which, see the Rollout Strategy
  hedge below); which Python version Railway's build actually resolves to
  (`RAILPACK_PYTHON_VERSION` is set on the live service, but that's a Railway
  project setting, not something pinned in this repo's own files — a
  from-scratch deploy elsewhere still has nothing here to pin it); whether
  `gunicorn` (pinned in `requirements.txt`) is actually what Railway invokes
  as the start command, as opposed to just being installed (Stack bullet
  above). (One thing that's *not* undecided, just unbuilt: there is no CI
  step ahead of a deploy, confirmed by `PIPELINE.md` §7 — a real test command
  now exists (`pytest`, see `TESTING.md`), but nothing runs it automatically,
  so a deploy today is still not blocked on tests passing. That's a settled
  fact about the current state, not an open question.)

## Rollout Strategy
Assumed direct deploy: whatever the actual trigger turns out to be (see "still
undecided" above — this repo doesn't confirm push-to-`main` vs. `railway up` vs.
a manual dashboard click), there's no canary/blue-green step in between — for
the web app, it's just one new Railway deployment replacing the running one.
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
down-step; no backup process is documented anywhere in this repo yet, so there's
currently no fallback if that manual step goes wrong.

It also does not cover Railway project/service settings (start command, env
vars set in the dashboard, etc.) — as noted in Deploy Steps, those live outside
git entirely. If a bad deploy was actually caused by a settings change rather
than a code change, `git revert` has nothing to revert; whoever changed the
setting has to change it back by hand.

## Post-Deploy Verification
No automated health checks or monitoring exist yet (see `OBSERVABILITY.md`) — and
this is now a real gap, not a bootstrap-time placeholder overtaken by later code:
the app is live in prod without one. Until one exists, verify a deploy manually:
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
