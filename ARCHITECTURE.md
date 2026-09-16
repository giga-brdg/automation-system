# Architecture

## Overview
A Python/Flask monolith (server-rendered Jinja templates, `Flask-SQLAlchemy`,
`Flask-Login`) that renders a registry of the company's automations and their ROI.
Self-service registration, role-gated login, and automation status/ownership pages are
already built and live — this is not a future-tense description. The Telegram bot itself
is already live too, for registration approval (see Components below); only *failure
alerts* over that same bot, plus token-spend tracking, are the two pieces still ahead —
see `docs/telegram_alerts_plan.md` and `ROADMAP.md`.

The Flask side is a handful of files under `src/`: `app.py` (routes plus all sync
orchestration), `models.py` (the SQLAlchemy models), `github_sync.py` (stateless
GitHub fetch/parse helpers), `telegram_bot.py` (the long-lived polling process) and
`telegram.py` (the send-only client it shares with the Flask process itself — see
the Telegram bot bullet below for which process boundary actually matters), and
`extensions.py` — just two lines, `db = SQLAlchemy()` and `login_manager =
LoginManager()`, the singletons every other module imports rather than constructing
their own. The view layer lives outside that list, in `src/templates/` (the Jinja
templates), `src/static/vendor/able-pro/` (the Bootstrap 5 / Able Pro design
system this app is built on, vendored from `github.com/giga-brdg/Design-system`,
internal-only license, see that folder's `VENDOR.md`), and `src/static/
supplax-overrides.css` (this app's brand palette and small first-party
helpers layered on top; `models.py`'s `Status.bs_variant` returns a Bootstrap
badge variant name that templates combine with that stylesheet's classes).

## Components
- **Web app (Flask + Jinja)** — server-rendered pages for the registry, ROI views, and
  a login-gated area with three roles (`Role.ADMIN`, `Role.AUTOMATOR`, `Role.VIEWER` in
  `src/models.py`). New accounts self-register, then need both an out-of-band
  confirmation code (`User.is_confirmed`) and admin approval via a Telegram
  `/grant <email> <role>` command (`User.is_approved`) before they can sign in — that
  same command is also what sets `User.role` (`src/telegram_bot.py`), so granting is
  how a Viewer becomes an Automator or Admin too, not just an approval gate. That
  rule is for the self-service path specifically: the `flask create-user` CLI (see
  See Also) creates an account with `is_confirmed`/`is_approved` already `True` and
  its role set directly from a `--admin` flag, bypassing both the confirmation code
  and `/grant` entirely — it's how the first Admin account gets bootstrapped, and
  it's the exception behind the Telegram bullet's "anyone but a bootstrapped admin"
  phrasing below. An Automator may only edit
  automations they own (`User.can_manage`) — see `docs/functions.md` for the full
  registration/role writeup and `SECURITY.md` for the security model. `AUTH_SECRET`
  signs sessions; unset in `.env`, the app falls back to an insecure hardcoded key
  (`src/app.py`), fine for local dev only.
- **HTTP routes (Flask)** — `/register`, `/login`, and `/confirm` are the three
  public, unauthenticated pages that get a new account signed in; only `/login`
  actually calls `login_user()` and creates the session — `/register` just writes
  `pending_code`/`is_confirmed` state and redirects, and `/confirm` just flips
  `is_confirmed` and redirects to `/login`. All three have to be public because
  account setup happens before any session exists, not because each one creates
  its own. `/`, `/automations`, and `/automations/<slug>` are the `@login_required`
  read routes that render the registry itself (index, list, and detail — the ones
  most users hit first). Write access is `/automations/new` and
  `/automations/<slug>/edit` (create/update an automation) plus `/departments/*`
  (rename/merge/delete, gated by a separate `@admin_required`, not just
  `@login_required`); `/automators/<id>` is `@login_required`-only, GET-only — an
  automator's profile page, with no create/update/delete operation exposed, so it
  doesn't belong lumped in with the create/update/delete routes above. `/skills` is
  the same GET-only shape for browsing the skills library, but it has one write
  sibling: `/skills/import-github` (`@login_required`, `@automator_required`) accepts
  two link shapes — a plain repo URL fetches `SKILL.md` from its root (one repo = one
  skill, mirroring the automation import shape below), while a GitHub folder URL
  (`.../tree/<branch>/<path>`, e.g. a shared repo's `.claude/skills/` — Supplax keeps
  one at `giga-brdg/Skills_Supplax`) bulk-imports every subdirectory under that path as
  its own skill, skipping (not failing the whole batch on) any subdirectory with no
  valid `SKILL.md` — and upserts each `Skill` row matched by name, so a second import
  of the same skill name updates that row instead of creating a duplicate. Two more
  routes reach out to GitHub for automations specifically —
  `/automations/import-github` (`@login_required`, `@automator_required`) and
  `/automations/<slug>/resync` (`@login_required`, plus an ownership check via
  `User.can_manage`) — these are what the "GitHub sync" bullet below describes; see
  there for what they fetch. Separately, there are two *inbound* API endpoints,
  authenticated per-user via an `X-API-Key` header (`User.api_key`) rather than
  session auth: `POST /api/automations/<slug>/sync`, which an external skill
  (`automation-portfolio-sync`) pushes a full metadata upsert to, and `GET
  /api/automations/<slug>/stage0-answers`, a read-only counterpart that lets
  `stage-0-supplax`'s own bootstrap step pull back whatever Stage 0 interview
  answers (`Automation.stage0_answers`, filled at `/automations/<slug>/stage0`,
  `src/stage0_questions.py`) were already entered in the dashboard, so it can skip
  re-asking. Neither endpoint calls out to GitHub; don't confuse either with the two
  routes above even though all four are colloquially "the sync."
- **Database** — SQLite at `data/portfolio.db` by default with zero config; set
  `DATABASE_URL` to point at Postgres instead (`src/app.py`). Schema code is written to
  work against either backend. Holds: automations (with a free-text description, a
  `stage0_answers` JSON blob for the Stage 0 interview keyed by phase number per
  `src/stage0_questions.py` — null until someone fills in `/automations/<slug>/stage0`
  — and `security_review_at`/`security_review_high`/`security_review_medium`, synced
  only from `dashboard/SECURITY_REVIEW.md`, never set any other way), a
  skills catalog (`Skill` — carries those same three `security_review_*` columns, via
  the shared `SecurityReviewMixin` both models inherit, synced from
  `{path/}dashboard/SECURITY_REVIEW.md` in the skill's own repo instead) and
  department-based categorization (`Department`, both
  many-to-many via join tables — there is no separate `Category` model, despite that
  term showing up in `ROADMAP.md`'s product framing), ownership, ROI metrics
  (`ROIEntry`), a GitHub link plus a `clickup_url` free-text field (see ClickUp note
  below), and relationships between automations (`Connection` — related/dependent, plus
  a free-text `shared_resource` column; already shipped and populated by the GitHub
  sync below, not merely planned — `ROADMAP.md`'s "Now" section is about product scope,
  not build status). Department is purely a categorization concept — it has no
  ownership semantics of its own (just `id`/`name`/`hue`); the actual ownership,
  `Automation.owner_id` / `User.can_manage`, is described under Components above.
  Department is also not part of the auth/role layer — the auth/role layer the
  login gate above actually depends on is
  just `User` and `Role`, and `Role` (`src/models.py` line 15) is a plain Python
  `enum.Enum` stored as a column value on `User.role`, not its own table the way
  `Department`/`Skill` are. The DB also holds `AutomationPage`, `AutomationTodoItem`,
  and `ReviewLogEntry` — fed by the GitHub sync in the next bullet — plus `Comparison`
  and `FeatureRow`, which that GitHub sync never touches; those two are written only by
  the *inbound* `POST /api/automations/<slug>/sync` endpoint described above
  (`api_sync_automation` in `src/app.py`).
- **GitHub sync** — live today, not future work, triggered by the two routes named
  above and by the `sync-github-org` CLI command (never by the inbound API endpoint).
  `sync_automation_from_github` (module-level in `src/app.py`, not nested in either
  caller — `sync-github-org` needed to reach it from `register_cli` too) holds every
  clear-vs-preserve decision for a single repo; `sync-github-org` itself is a thin loop
  over `github_sync.list_org_repos()` that calls that same function per repo, with a
  stricter bar than the single-repo routes (skips a repo outright if it has no
  `dashboard/SUMMARY.md`, rather than falling back to README). `src/github_sync.py`
  itself is just stateless fetch/parse helpers (`fetch_raw_file`, `list_org_repos`,
  `parse_readme`, `parse_roi_md`, `summary_fields_from_sections`, `parse_functions_md`,
  `parse_todo_md`, `parse_backlog_md`, `security_review_fields_from_sections`) with no
  orchestration of its own. On sync it fetches and parses `README.md` (name/one-liner
  fallback), `dashboard/ROI.md`, `dashboard/SUMMARY.md`, `dashboard/functions.md`,
  `dashboard/TODO.md` (falling back to root `TODO.md`), `backlog/BACKLOG.md`, and
  `dashboard/SECURITY_REVIEW.md` from the automation's own repo into `Automation`,
  `ROIEntry`, `AutomationPage`, `Connection`, `AutomationTodoItem`, and
  `ReviewLogEntry` rows, with specific clear-vs-preserve rules per file (e.g. an empty
  `## Pages` section only clears stale pages when `SUMMARY.md` was actually fetched).
  `dashboard/SECURITY_REVIEW.md` is a record of the last time someone ran Claude Code's
  built-in `/security-review` command against the repo (diff-scoped, not a full-codebase
  audit) — its absence sets `security_review_at` to `None`, which `security_review_state`
  (`SecurityReviewMixin`, `src/models.py`) surfaces honestly as "не перевірено" rather
  than defaulting to "clean". The exact same file convention (and mixin) covers `Skill`
  too — `sync_skill_from_github`/`sync_skills_from_github_folder` fetch
  `{path/}dashboard/SECURITY_REVIEW.md` from the skill's own repo (or subdirectory, for
  a shared multi-skill repo) the same way — and `src/templates/_security_chip.html` is
  the one shield-icon macro both `Automation` and `Skill` cards/detail panels render it
  through, so the four states (`none`/`clean`/`medium`/`high`) look identical everywhere.
  Nothing in this app can trigger `/security-review` itself, sync only ever reads
  whatever record a human/Claude session already
  wrote and pushed.
  `dashboard/SUMMARY.md`'s `## Skills` bullet list links the automation against the
  skills library (`/skills`) by exact name match — unlike `## Departments`, an
  unmatched skill name is skipped with a warning rather than auto-creating a bare
  library entry, since the library is a curated catalog, not free-text tags. Needs
  `GITHUB_TOKEN` in `.env` to read private repos.
- **ClickUp** — not an active integration. `clickup_url` is a plain field set manually
  or via the sync payload above; there is no ClickUp fetch/parse code anywhere in `src/`.
- **Telegram bot** (`src/telegram_bot.py`) — live today and load-bearing for the
  registration-approval flow (`/grant`, `/revoke`) described above, not a future
  addition. It is a second, independently-run long-lived polling process
  (`python -m src.telegram_bot`, per `README.md`), not part of the Flask process — it
  has to be deployed and kept alive alongside the web app, or both `/grant` (and
  therefore login for anyone but a `create-user`-bootstrapped admin) and `/revoke`
  stop working. `src/telegram.py` is a separate, send-only client shared by this
  process and the Flask process itself: `/register`'s handler calls it directly,
  in-process, to send the admin the confirmation code, so that send does *not*
  depend on `telegram_bot.py` being up — only `/grant`/`/revoke` do. Needs
  `TELEGRAM_BOT_TOKEN` and `ADMIN_TELEGRAM_CHAT_ID` in `.env`.
- *(Next/Later)* Automated failure alerts over the same Telegram bot; token-usage
  tracker. See `docs/telegram_alerts_plan.md`.

## Data Flow
A user self-registers (confirmation code + admin `/grant <email> <role>` approval, which
also assigns their role) → once logged in as an Automator or Admin, they register an
automation by hand at `/automations/new`, then bring in its metadata and ROI figures one
of two ways: triggering an automated fetch-and-parse from the automation's own GitHub
repo (`/automations/import-github` or `/resync` — GitHub sync, above; nothing beyond
clicking the button is typed in by hand) or via the `automation-portfolio-sync` skill
pushing a full metadata upsert to the inbound `POST /api/automations/<slug>/sync`
endpoint → the dashboard reads that data and renders the registry + ROI cards for whoever
is logged in, scoped by role.

## Key Decisions
- ADR-0001 — recorded a Next.js + Postgres + monolith plan for v1. **Superseded in
  practice**: the shipped app is Flask, not Next.js, and defaults to SQLite (Postgres
  is opt-in via `DATABASE_URL`). ADR-0001 itself has not been updated to reflect this;
  treat its stack section as historical, not current. See
  `docs/adr/0001-stack-and-architecture.md`.
- Schema evolution: no migration framework. `flask migrate-registration` (`src/app.py`)
  is a one-off command hard-coded to the four `user` columns self-service registration
  needed (`is_confirmed`/`is_approved`/`pending_code`/`pending_code_expires_at`); it
  introspects existing columns first, so it's safe to re-run, but it is not a general
  schema-evolution facility. The established pattern for a *future* schema change is a
  brand-new, separately-named CLI command with its own idempotent `ALTER TABLE` list
  (see `docs/telegram_alerts_plan.md`'s phase-1 instructions), not extending
  `migrate-registration` itself.

## Scale & Constraints
Expected scale: medium — built to handle company-wide usage and a growing automation
count, not just a handful of records, but no high-traffic/high-availability design
work is warranted yet.

## See Also
`SECURITY.md`, `DEPLOYMENT.md`, `OBSERVABILITY.md`, `TESTING.md`, `GLOSSARY.md`. For local
setup and bootstrap (the `init-db`, `create-user`, and `seed-demo` CLI commands, and which
`.env` values are actually required), see `README.md` — not `docs/getting-started.md`,
which is stale (written for an earlier Node.js/Postgres stack the project no longer uses)
and, unlike ADR-0001 above, is not marked as superseded anywhere in its own text.
