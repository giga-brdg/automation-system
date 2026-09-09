# Testing

## Strategy
The live app (`src/`, ~1,500 lines) is Flask/Python — Flask, Flask-SQLAlchemy,
Flask-Login, psycopg2-binary (see `requirements.txt`) — not the Next.js/TypeScript
stack `ADR-0001` still records as the original plan. (`ARCHITECTURE.md` has
already been corrected to describe Flask — it is not stale; only the ADR
itself still shows Next.js, kept deliberately as a historical record: an ADR
is expected to preserve what was decided at the time, not to track what's
true now, which is a different job from `CONTRIBUTING.md`'s setup steps
flagged near the end of this file — those are actionable instructions a
newcomer actually runs, so a stale stack reference there sends them down the
wrong path instead of just documenting history.) The
datastore is SQLite by default (`data/portfolio.db`, zero config) with
Postgres as an opt-in upgrade via `DATABASE_URL` (`ARCHITECTURE.md`,
`src/app.py`) — not Postgres-by-default, so tests shouldn't assume a Postgres
fixture is required. Testing strategy has to follow the code that actually
exists, not either stale premise:
- **Unit** — pure, no-mocking-needed logic, in two places: `src/models.py`'s
  small helpers (`Role.label`/`Status.label`/`dot_color`, `hue_for`,
  `User.is_admin`/`is_automator`/`can_manage`/`initials`,
  `Department.pill_style`), and — the bigger target by line count, and the
  cheapest to cover well since none of it needs mocking — `src/github_sync.py`'s
  markdown-parsing functions (`parse_readme`,
  `parse_markdown_sections`, `parse_functions_md`,
  `summary_fields_from_sections`, `parse_pages_section`, `parse_backlog_md`,
  `parse_todo_md`, `roi_fields_from_sections`, `parse_skill_md`): each takes a string in and
  returns data out, with no GitHub API call anywhere in them, so they're
  ideal unit-test candidates that need zero mocking. (There's no dedicated
  ROI-calculation method anywhere in the code — `ROIEntry` in
  `src/models.py` is a plain data record with no computed fields; the "ROI"
  logic worth testing is this parsing, which fills that record from a
  repo's markdown.) The app's Flask CLI commands (`src/app.py`: `init-db`,
  `create-user`, `migrate-registration`, `seed-demo`) belong on this list
  too and are currently untested — `migrate-registration` especially:
  it's idempotent and introspects existing columns before altering
  anything, exactly the deterministic, no-network shape this bullet is
  about, just packaged as a CLI command instead of a plain function.
- **Integration** — Flask routes in `src/app.py` against a disposable SQLite
  database (an in-memory `sqlite:///:memory:` or a temp-file DB per run — no
  Postgres server needed for this). Watch out: `create_app()` (`src/app.py`)
  takes no test-config argument and reads `DATABASE_URL` from the
  environment at call time, defaulting to the real `data/portfolio.db` file
  when unset — a fixture has to set `DATABASE_URL` (or otherwise override
  `SQLALCHEMY_DATABASE_URI`) *before* calling `create_app()`, or an
  integration test silently reads/writes the actual local dev database.
  This ordering requirement comes from `create_app()`'s own code, not from
  whichever test framework ends up wrapping it, so it holds regardless of
  the still-open `pytest` vs. something-else decision below.
  Separately, the parts of the two external integrations that actually hit
  the network need faking with `unittest.mock` (patch the functions
  themselves) rather than being hit for real: GitHub sync's
  `fetch_latest_commit` / `default_branch` / `fetch_raw_file`
  (`src/github_sync.py` — not the parsing functions above, which don't touch
  the network) and the Telegram bot approval/registration flow
  (`src/telegram_bot.py`, `src/telegram.py`) — a fabricated GitHub commit
  JSON response, and a fabricated Telegram API payload. Don't reach for the
  `responses` library here: both `github_sync.py` and `src/telegram.py`
  say in their own docstrings that they use stdlib `urllib.request`
  exclusively, never `requests`, and `responses` only intercepts
  `requests`-based traffic — pointed at this codebase it would silently
  intercept nothing rather than fake anything.
  The most branch-heavy piece of logic worth its own dedicated integration
  scenarios, not just "fake the network and move on," is
  `sync_automation_from_github` in `src/app.py` (and the `POST
  /api/automations/<slug>/sync` endpoint that also calls it): it holds
  every clear-vs-preserve decision for a re-sync (`ARCHITECTURE.md` — e.g.
  an empty `## Pages` section only clears stale pages when `SUMMARY.md` was
  actually fetched), so it needs cases for each of those branches, not just
  one happy-path fetch.
  Most authenticated routes worth integration-testing sit behind
  `@login_required`/`@automator_required`, but the only account-creation
  path this doc or `README.md` documents is the `create-user` CLI, which
  README flags as interactive-only (hidden, confirmed password prompt —
  "don't expect to script it without a pty"). For tests, create a user
  directly the same way `create-user` does internally: `User(...,
  is_confirmed=True, is_approved=True)` plus `user.set_password(...)`
  (`src/app.py`'s `create_user` command, `src/models.py`) — no CLI, no TTY,
  no prompt.
- **End-to-end** — core dashboard flows: login, view registry, view an
  automation's ROI. A freshly bootstrapped instance has an empty registry
  (see "How to Run" below), so this needs at least one automation added or
  seeded first — via the manual "add automation" form or README's
  `seed-demo` CLI command — or the "view an automation's ROI" step has
  nothing to click on. Registration/approval is only partly E2E-testable
  this way: the confirmation-code step runs in-app, but completing approval
  needs an admin's Telegram `/grant` reply, which requires
  `src/telegram_bot.py` running as its own long-lived process (see
  `DEPLOYMENT.md`) — either fake that Telegram round-trip the same way
  Integration does, or treat the admin-approval half as a manual check
  rather than a true automated E2E step.

**Framework: `pytest`**, decided and wired up (`requirements.txt`, `tests/`) —
plain `pytest`, no `pytest-flask`. Fixture/teardown mechanics for the
disposable database (`tests/conftest.py`): a **temp-file SQLite DB per test**,
not `:memory:` — a plain SQLAlchemy engine opens a new connection per
checkout, and `:memory:` doesn't survive that across requests without extra
pooling config a temp file avoids needing. The `app` fixture sets
`DATABASE_URL`/`AUTH_SECRET` before importing/calling `create_app()`
(the ordering requirement two paragraphs up), yields the app inside an
app context after `db.create_all()`, then disposes the engine before
unlinking the file — Windows keeps a file handle open through the engine's
connection pool even after `db.session.remove()`, so skipping the dispose
step fails the temp file's cleanup with a `PermissionError` on that platform.

End-to-end still has no framework decision — Playwright (drives a running
server over HTTP, independent of the server-side template stack) remains
the candidate, not yet confirmed or added to `requirements.txt`. Nothing in
`tests/` today is E2E; see What's Not Covered Yet.

## How to Run
```
pytest
```
from the repo root runs everything in `tests/` (`tests/README.md` names this as
its pair). No config flags needed — `pytest.ini`/`pyproject.toml` settings
aren't required for the current suite (`tests/conftest.py` handles its own
database setup per test via fixtures, described above).

This covers the auth/registration surface (see What's Not Covered Yet for what
it doesn't). For anything outside that — a UI change, a GitHub-sync edit, a
CLI command — the only way to verify is still manual: follow root
`README.md`'s "Install" and "Usage" sections to get a local copy running
(`python -m src.app` for the web app, plus `python -m src.telegram_bot` in a
second process — registration approval needs both). Don't use `src/README.md`
for this: it's a one-paragraph note on what belongs in that folder, with no
run steps. `DEPLOYMENT.md` covers the Railway/prod deploy specifically, not
local running — but it still documents general app behavior that applies
locally too, which is why it's cited below for the `ADMIN_TELEGRAM_CHAT_ID`
failure mode: that's a fact about the app, not about Railway. Set `TELEGRAM_BOT_TOKEN` and `ADMIN_TELEGRAM_CHAT_ID` in `.env` first (see
`.env.example`; `AUTH_SECRET` can stay unset locally, and so can
`GITHUB_TOKEN` — per `DEPLOYMENT.md` it's "genuinely optional," its absence
just means unauthenticated, rate-limited GitHub calls, nothing that blocks
this walkthrough). Without `ADMIN_TELEGRAM_CHAT_ID` specifically, the
failure is loud, not silent: `telegram_bot.py`'s `run()` calls `raise
SystemExit("ADMIN_TELEGRAM_CHAT_ID не задано в .env")` the instant you
launch that second process (above), so the whole approval-bot process refuses
to start with a printed error rather than quietly doing nothing
(`DEPLOYMENT.md` says the same once you read past its first bullet — its
next bullet contrasts this with `AUTH_SECRET`/`TELEGRAM_BOT_TOKEN`, which
*do* fail silently). If instead the bot process is running but an admin
never gets a Telegram message, suspect a missing `TELEGRAM_BOT_TOKEN`:
`src/telegram.py` returns `None`/`False` from every call when it's unset,
with no error at all — that's the actually-silent failure mode.

Then, before any of that is clickable: create the schema (`flask --app src.app init-db`) and the first
account, since self-service registration needs an existing admin to approve
it (`flask --app src.app create-user <email> <name> --admin`, interactive
password prompt) — both are in README's "Usage" section but easy to miss if
this file is followed on its own. Optionally, `flask --app src.app seed-demo
<owner_email>` fills the registry with sample data so there's something to
view (a fresh instance otherwise has zero automations). Then click through
login, registration, and the registry/ROI views by hand.

## Coverage
No coverage tooling or numeric target exists yet — 17 tests exist
(`tests/test_auth_security.py`) but nothing measures what fraction of the
codebase they actually exercise. Once a target is decided, it should account
for the fact that coverage today is concentrated on one surface (auth), not
spread evenly - see What's Not Covered Yet.

## What's Not Covered Yet
**Now covered** (`tests/test_auth_security.py`, 17 tests, added in the
production-hardening pass that also fixed each of these): CSRF protection on
every POST route, `/confirm`'s brute-force lockout (5 wrong attempts
invalidates the code), `api_key` rotation on `/grant`/`/revoke` and the
self-service regenerate route, the `/automators/<id>` email-disclosure fix,
and `POST /api/automations/<slug>/sync`'s `is_approved` check (plus its CSRF
exemption). This was the single highest-risk gap this section used to flag —
the live app's public, unauthenticated `/register` surface had zero automated
coverage — so it's what got covered first, per this section's own
previously-stated priority.

**Still not covered, and genuinely still a gap:**
- `src/github_sync.py`'s markdown-parsing functions (`parse_readme`,
  `parse_markdown_sections`, `summary_fields_from_sections`, `parse_skill_md`,
  etc.) — the Strategy section's "cheapest to cover well" candidates, zero
  mocking needed, still untested.
- `sync_automation_from_github`'s branch logic (clear-vs-preserve on re-sync)
  and the GitHub-sync/Telegram network-facing code paths generally (need the
  `unittest.mock` fakes the Strategy section describes).
- Every route outside the auth surface above: automation CRUD, skills
  library, departments, the token-usage panel/CLI, GitHub import/resync.
- The Flask CLI commands beyond what the auth tests exercise indirectly
  (`init-db`, `create-user`, `seed-demo`, `migrate-*`, `check-token-usage`).
- End-to-end flows (no Playwright, or any E2E tooling, added yet).

CI is not wired up yet either (see `PIPELINE.md` §7) — a real test command
(`pytest`) exists now, but nothing runs it automatically before merge. CI
should require it to pass — the project's stated v1
priority is long-term maintainability over shipping fast, which favors a
blocking gate over an advisory one. Note that a pass/fail gate alone only
enforces that tests exist and pass, not their depth — deciding the (still
unset) coverage target from the section above is a separate, later decision
that the gate doesn't substitute for.

Separately: `CONTRIBUTING.md` and `README.md` don't yet mention running tests as
part of the contribution workflow — once a command exists here, that should be
added there too (out of scope for this file to fix). Worse, and also out of
scope here: `CONTRIBUTING.md`'s "Local dev setup" still tells a newcomer to
install Node.js/npm and links `docs/getting-started.md`, which is entirely
`npm install`/`npm run dev` instructions for the abandoned Next.js stack —
both need the same Flask/Python correction this file just got, or a stranger
sets up the wrong toolchain before ever reaching the test suite.
