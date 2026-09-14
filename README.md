# Automation ROI Dashboard

Internal Supplax dashboard that lists every company automation — what it does, which
skills/tools it uses, who owns it, and its ROI (time/money saved, status) — so
leadership and employees can see the value and health of automation work in one
place.

The instructions below are for setting up a local development copy. The app itself is
already live in production (deployed to Railway) — see `DEPLOYMENT.md` for how that
deploy, rollback, and verification actually work. This file only covers running a copy
on your own machine.

## Install
```bash
python -m venv .venv
source .venv/bin/activate    # macOS/Linux; on Windows PowerShell see the note below instead
pip install -r requirements.txt
cp .env.example .env
```
Python 3 (no version pinned in the repo). **Edit `.env` now**, before the next step —
every variable below is read once at app startup, so a later edit needs a process
restart to take effect. `DATABASE_URL` specifically also needs to be right *before*
`init-db` runs: it targets the schema-creation, and changing it afterward won't
retarget what `init-db` already created against the old value (re-run `init-db`
against the new one if you do change it later). No database server is required to
start: `DATABASE_URL` defaults to a local SQLite file (`data/portfolio.db`) — leave it
empty in `.env` to use that; Postgres is optional, point `DATABASE_URL` at one only if
concurrent writes become a real problem, then re-run `init-db`. `GITHUB_TOKEN` is
genuinely optional, not required: GitHub API calls in `src/github_sync.py` work
unauthenticated against any public repo, just at GitHub's lower unauthenticated rate
limit; it's only actually needed to read a *private* repo, or if you're hitting that
rate limit. `TELEGRAM_BOT_TOKEN` + `ADMIN_TELEGRAM_CHAT_ID` do need real values for
the registration-approval flow described below to work at all — but the failure when
either is missing isn't silent: `/register`'s handler (`src/app.py`) still saves the
registration either way, then flashes a visible warning on the page itself ("не
вдалося сповістити адміністратора в Telegram — звернись до нього напряму") whenever
`telegram.send_message` comes back falsy, which is exactly what happens with no
`ADMIN_TELEGRAM_CHAT_ID` to send to. See `DEPLOYMENT.md`'s Env vars section and
`TESTING.md`'s How to Run section for the fuller picture of which failure modes are
loud vs. silent.
`AUTH_SECRET` is optional for local dev — the app falls back to an insecure hardcoded
key if it's unset (`src/app.py:42`) — but set it for anything beyond your own machine.
`CLICKUP_API_TOKEN` is declared in `.env.example` but no code currently reads it;
ClickUp linkage today is just a plain URL field on the add-automation form, not an
automatic sync, so leave it empty.

Once `.env` is set the way you want it, create the schema:
```bash
flask --app src.app init-db
```

**Windows PowerShell**: the block above uses `source .venv/bin/activate`, a
macOS/Linux command — on Windows, activate with `.venv\Scripts\Activate.ps1` instead
(if script execution is disabled, run `Set-ExecutionPolicy -Scope Process
RemoteSigned` first). Don't use the bare `.venv\Scripts\activate` some Windows docs
show either — that's the cmd.exe/bash shim and won't run under PowerShell. PowerShell
also has no `&&` chaining — run each line of the block above as its own command rather
than joining them.

## Usage
```bash
python -m src.app
```
Runs the Flask dev server at `http://127.0.0.1:5000/`. Separately, run the
registration-approval bot (needed before anyone but the first admin can log in):
```bash
python -m src.telegram_bot
```
The first account has to be created directly, since self-service registration needs
an admin to approve it:
```bash
flask --app src.app create-user <email> <name> --admin
```
This prompts interactively for the password (hidden input, typed twice to confirm) —
it's not a one-shot non-interactive command, so don't expect to script it without a
pty.

After that, other people can sign up at `/register`. That route itself — not the bot
process — generates a confirmation code and sends it straight to the admin's Telegram
chat via a direct HTTPS call to the Telegram Bot API (`src/telegram.py`), so the
notification fires whether or not `python -m src.telegram_bot` happens to be running
(it just silently doesn't fire at all if `ADMIN_TELEGRAM_CHAT_ID` is unset — see
Install above). The admin relays that code to the registrant out-of-band; the
registrant enters it at `/confirm` to mark the account confirmed. Only once an account
is confirmed does the bot's `/grant <email> <role>` succeed (role: `admin`,
`automator`, or `viewer` — see below for what each can do) — it explicitly refuses to
grant access to an account that hasn't confirmed its code yet. The bot process is what
actually needs to be running for that step, and for `/revoke`. Once granted, the
account logs in at `/login` (the app's `/` also redirects there when logged out). To
see the dashboard with sample data instead of an empty list, run `flask --app src.app
seed-demo <owner_email>` after creating that owner.

Once logged in, an `admin` or `automator` can register a single automation by pulling
it from a repo at `/automations/import-github` (reads that repo's `README.md` plus its
`dashboard/ROI.md`, `dashboard/SUMMARY.md`, `dashboard/functions.md`,
`dashboard/TODO.md`, `backlog/BACKLOG.md`, and `dashboard/SECURITY_REVIEW.md` — see the
`automation-portfolio-sync` skill for what a repo needs before that import produces a
complete record). `dashboard/SECURITY_REVIEW.md` is optional and not generated by
anything — it only exists once someone has actually run Claude Code's built-in
`/security-review` against the repo and written the result there; its absence shows as
"Не перевірено" on the automation's page, not an error. An
existing automation's "Оновити з GitHub" button (`/automations/<slug>/resync`) repeats
that same import against the repo URL already on file. A `viewer` can only browse;
only an `admin`, or the `automator` who owns that automation, can add, edit, or resync
it (`User.can_manage` in `src/models.py`).

There's no manual "add from scratch" form anymore — every automation's record comes
from a real repo. For registering many at once, `flask --app src.app sync-github-org
<owner>` (e.g. `giga-brdg`) imports every repo under that GitHub org/user that has a
`dashboard/SUMMARY.md`, skipping the rest (an SDK repo, this dashboard's own repo,
anything archived) rather than guessing which ones count. An automation this command
creates for the first time is assigned to `AUTOMATION_SYNC_OWNER_EMAIL`; one that
already exists here keeps whoever already owns it. Runs daily on its own Railway cron
service, `github-org-sync-cron` (see `DEPLOYMENT.md`), same as `check-token-usage` —
running it by hand works too.

Registering a brand-new automation by hand redirects straight to
`/automations/<slug>/stage0` — the same 9-phase interview `stage-0-supplax` runs live
in a Claude Code session (`src/stage0_questions.py`), fillable here instead so the
answers exist before the repo is even bootstrapped. It's skippable (there's a
"Пропустити" link back to the automation's page) and can be revisited later from that
page's "Stage 0" button.

There's also a machine-facing route for CI/automation pipelines to push a record
without a browser session: `POST /api/automations/<slug>/sync`, authenticated with an
`X-API-Key` header carrying an `admin`'s or `automator`'s personal `api_key` (printed
once, in `create-user`'s CLI output — there's no UI to view or regenerate it, so save
it then). This is what `stage-0-supplax`'s portfolio-sync step actually calls. The
handler's own docstring (`src/app.py`) points to `references/portfolio-sync.md` for
its payload schema, but that file doesn't exist in this repo yet — read the handler
itself (`api_sync_automation` in `src/app.py`) for the current field list until it
does. Its read-only counterpart, `GET /api/automations/<slug>/stage0-answers` (same
`X-API-Key` auth), is how `stage-0-supplax`'s bootstrap step pulls back whatever Stage
0 answers were already filled in above, so it can skip asking about them again.

There's no automated test suite yet (`TESTING.md`: no test command exists to run) —
verify a local change manually by clicking through login, registration, and the
registry/ROI views.

See [docs/getting-started.md](docs/getting-started.md) for the full setup (currently
stale — written for an earlier Node/Postgres stack the project no longer uses; needs
the same correction as this file).

## License
Proprietary — internal Supplax project. See [LICENSE](LICENSE).
