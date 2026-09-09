# Security Policy

## Scope
Covers this repo's app, as four separate surfaces:
- the login/registration flow (`src/app.py`'s `register()`/`login()`/`confirm()`);
- the Telegram admin-approval bot (`src/telegram_bot.py`);
- the API-key-authenticated automation-sync endpoint (`POST
  /api/automations/<slug>/sync`, `src/app.py` lines ~556-570) — a third,
  machine-facing auth surface, separate from the session-cookie login above,
  keyed off the per-user `api_key` column in `src/models.py`;
- the GitHub-sync integration (`src/github_sync.py`, invoked by
  `/automations/import-github` and `/automations/<slug>/resync` in
  `src/app.py`). This is **not** part of the login/registration flow despite
  living in the same file — `register()`/`login()`/`confirm()` never call into
  `github_sync`, and reaching it at all already requires an authenticated
  Automator/Admin session (`@automator_required`, plus `User.can_manage` on
  resync) supplying a repo URL. Its own risk is different from auth: it fetches
  and parses README/ROI/SUMMARY/BACKLOG/TODO content from that
  admin/automator-supplied URL (restricted to `github.com`/
  `raw.githubusercontent.com` by `github_sync.REPO_URL_RE`) using `GITHUB_TOKEN`
  — see the Known Limitations entry below for that token and the untrusted-input
  angle.

ClickUp is **not** in scope because there is no ClickUp integration to cover:
`clickup_url` (`src/models.py`) is a plain link a user types into a form, and
`CLICKUP_API_TOKEN` (`.env.example`) is never read by any code in this repo
(`ARCHITECTURE.md` confirms it's "not an active integration"). Does not cover
the other Supplax automations this dashboard links to — report issues in those
in their own repos. For what the login/registration and approval flow actually
do end to end, see `README.md`'s Usage section (`/register` → Telegram notifies
the admin → `/grant`).

## Status: this is live, not a future plan
Earlier drafts of this file called external (beyond-corporate-network) login
access "planned." It has since shipped: the login/registration flow and the
Telegram approval bot are built and in production use (`dashboard/SUMMARY.md`'s
Status field reads "live" — note that field is regenerated from
`docs/functions.md` via `automation-portfolio-sync`, not pulled from deployment
telemetry, so treat it as a self-report, the same epistemic caution given to
`AUTH_SECRET` below. The 2026-09-01 entry in `backlog/BACKLOG.md` says the same
thing, but that isn't independent corroboration: that entry's own text
("перегенерував dashboard/SUMMARY.md і dashboard/functions.md, статус проєкту
змінив на 'Працює'") shows the status field and the backlog note were written
in the same sync edit, not observed separately — so it doesn't add confidence
beyond the self-report itself). This surface is real today — see `PIPELINE.md` §5, which
now carries its own 2026-09-02 correction making this same point: its original
framing ("audience stays internal-only, only the network perimeter is not
trusted") is marked stale there too, not just here, so this file and §5 agree
rather than one lagging the other. The detail both now flag: registration
itself is self-service and open to anyone who reaches `/register`
(`dashboard/SUMMARY.md`: "Завести акаунт може будь-хто" — anyone can create an
account). Access is gated by admin approval *after* signup, not by restricting who
can submit a request, so treat the registration endpoint as public attack surface,
not merely "employees connecting remotely." Separately, `PIPELINE.md` §5 also
flags that this same self-service registration now collects employee personal
data (name, email, department, skills) and that whether this triggers any
compliance obligation is unresolved — this file doesn't answer that question,
it's noted here only because it follows directly from the same feature.

## Supported Versions
| Version | Supported |
|---|---|
| main | yes |

"Supported" means reports against `main` get triaged and, if confirmed, patched by
the project owner — solo maintainer, best-effort, no formal SLA yet. There's no
older release line to backport to.

## Known Limitations
Known gaps identified so far, grouped by component — not exhaustive, and not
capped at one per component. A production-hardening pass (see the entries
marked **Fixed** below) closed six of these; the rest are still open,
including one — the Telegram bot's chat-ID-only auth — that pass looked at
and deliberately left as accepted risk rather than an oversight.

`flask --app src.app migrate-confirm-attempts` (the `/confirm` fix's required
manual step, below) **has been run against production** — confirmed live: a
real registration through `/register` no longer 500s, and five wrong
`/confirm` codes in a row correctly triggers the lockout message, both
checked directly against the deployed app after the migration ran.

- **Telegram bot** (`src/telegram_bot.py`): authorizes purely by the incoming
  message's chat ID matching `ADMIN_TELEGRAM_CHAT_ID` — the bot token itself
  carries no separate access control (see the module docstring). The two leaks
  are not equally severe, though: the **bot token** is immediately usable if
  leaked (anyone holding it can call the Telegram Bot API directly — poll
  updates, send messages as the bot), while the **chat ID** alone is not
  spoofable through that API (Telegram sets `chat_id` from the real
  originating chat), so knowing the number does nothing without also
  controlling that actual Telegram account/chat. Either compromise is still
  serious: `/grant <email> admin` (lines 49-65) hands **full `Role.ADMIN` over
  the entire app** to any account named in the command — a materially worse
  outcome than "compromises the approval flow," since it's not just approval
  gate bypass but a direct route to admin-level control of every automation,
  user, department, and skill in the app. **Still open, deliberately**: the
  production-hardening pass that fixed the entries below looked at this one
  and left it as accepted risk rather than an oversight — a second factor
  beyond "controls the actual Telegram chat" would need real added
  infrastructure (a shared secret, a second approval channel) for a
  solo/small-team admin flow, which was judged out of scope for that pass.
  Revisit if the admin team grows beyond one or two trusted people.
- **GitHub-sync** (`src/github_sync.py`): reads `GITHUB_TOKEN` (`.env`) to call
  the GitHub API on an authenticated Automator/Admin's supplied `repo_url`
  (`/automations/import-github`, `/automations/<slug>/resync` — see Scope
  above). No scope is documented for this token beyond "needed for private
  repos" (`ARCHITECTURE.md`); if it's a broader-scoped PAT than this app
  actually needs, leaking it (logs, error output, the process environment)
  gives an attacker that broader access, not just what this app uses. The
  fetch target itself is constrained to `github.com`/
  `raw.githubusercontent.com` by `github_sync.REPO_URL_RE`, so this isn't an
  open SSRF vector — but the fetched README/ROI/SUMMARY/BACKLOG/TODO content
  is still attacker-influenceable if the supplied repo isn't trusted, and gets
  parsed and stored into `Automation`/`ReviewLogEntry`/`AutomationTodoItem`
  rows; templates don't use Jinja's `|safe` on it (checked), so this isn't a
  known stored-XSS path today, but it hasn't been reviewed as untrusted input
  beyond that.
- **Authorization model / IDOR — Fixed.** Three roles (`Role.ADMIN`/
  `AUTOMATOR`/`VIEWER`, `src/models.py`) gated by `admin_required`/
  `automator_required`/`User.can_manage` (`src/app.py`) describe who can do
  what once logged in, but `/automators/<int:user_id>` was gated only by
  `@login_required` with no role check, and `automator_profile.html` printed
  `automator.email` directly — so any authenticated user, including a
  self-registered `VIEWER`, could enumerate sequential user IDs and harvest
  every other user's email. `automator_profile.html` now only renders the
  email when the viewer is an admin or viewing their own profile; the rest of
  the page (name, role, automations list) is unchanged and still visible to
  any logged-in user, since that part was never the actual PII leak. This was
  the live path feeding the compliance question the Status section above
  raises about self-service registration collecting employee personal data.
- **Session cookie has no explicit transport flags — Fixed.**
  `SESSION_COOKIE_HTTPONLY` and `SESSION_COOKIE_SAMESITE=Lax` are now always
  set. `SESSION_COOKIE_SECURE` is tied to whether `RAILWAY_ENVIRONMENT` is
  set (`True` in any real Railway deployment, `False` for a plain local
  `python -m src.app` over http, which would otherwise never get the cookie
  back) rather than hardcoded — confirmed via the live Railway environment
  that `RAILWAY_ENVIRONMENT` is in fact set there today, so this resolves to
  `True` in production, not just in theory.
- **Login/registration — `AUTH_SECRET` fallback.** `SECRET_KEY` (which signs
  the Flask-Login session cookie) is set from `AUTH_SECRET`, but with a
  silent fallback to a hardcoded insecure key if that env var is missing —
  no startup check refuses to run without it set. **Verified, not just
  assumed**: the live Railway environment variable is in fact set to a real,
  non-default value (confirmed directly against the deployed environment;
  the value itself is deliberately not repeated here or anywhere else in this
  repo). The code-level gap — no startup check enforcing this — is still
  open; today's safety is an operational fact about the current Railway
  config, not something the code itself guarantees going forward.
- **Login/registration — rate limiting — Fixed (both `/confirm` and
  `/login`).** `/confirm`: the registration code is a 6-digit number valid
  for 30 minutes, and past 5 wrong attempts (`User.pending_code_attempts`)
  the code is now invalidated outright, forcing a fresh `/register` for a
  new one, rather than staying guessable for the rest of its 30-minute
  window. `/login`: online password guessing is now throttled via
  Flask-Limiter — 10 `POST` attempts per minute per client IP (loading the
  form itself, `GET`, is never limited). Getting the real client IP right
  behind Railway's edge proxy needed `werkzeug`'s `ProxyFix`, trusting
  exactly one `X-Forwarded-For` hop — applied only when `RAILWAY_ENVIRONMENT`
  is set, so a local dev instance isn't trusting a header nothing real is
  actually adding, which would otherwise let a local request spoof its rate-
  limit identity. Storage is Flask-Limiter's in-memory backend, deliberately:
  the live `web` service is confirmed single-instance (see `DEPLOYMENT.md`),
  so there's no second process with its own separate counter to disagree
  with — revisit (a shared store) only if that changes. A throttled request
  gets a plain flash ("Забагато спроб входу — зачекай хвилину і спробуй ще
  раз."), not a raw error page.
- **Login/registration — no CSRF protection — Fixed.** Flask-WTF's
  `CSRFProtect` is now wired up app-wide (`src/app.py`); every state-changing
  form (`/automations/new`, `/automations/<slug>/edit`, department/skill
  actions, login/register/confirm, the API-key regenerate action) carries a
  `csrf_token` field. The one deliberate exception is `POST
  /api/automations/<slug>/sync`, explicitly exempted (`@csrf.exempt`) since
  it's machine-facing (`X-API-Key` header auth, no Flask session to carry a
  token in the first place) — that's a scoped exemption, not a gap.
- **API-key sync endpoint — Fixed.** `POST /api/automations/<slug>/sync`
  authenticates by a per-user `api_key` (`secrets.token_hex(32)`), and that
  key was live from the moment of self-registration — before `/confirm`,
  before Telegram `/grant`, before any admin action — sitting dormant only
  because the endpoint also required `owner.role in (Role.ADMIN,
  Role.AUTOMATOR)`; a single chat-ID-gated `/grant <email> automator` flipped
  that role without ever regenerating or displaying the key, silently
  activating a credential the account already held. `/grant` now rotates
  `api_key` and relays the new value in its Telegram reply (only for
  `automator`/`admin` — a `viewer`'s key is inert regardless, no need to
  surface it). Revocation was also narrower than it looked: `/revoke` only
  ever set `is_approved = False`, and this endpoint never checked that flag —
  only `role` — so a revoked Automator/Admin's key kept authenticating after
  `/revoke`. The endpoint now also requires `owner.is_approved`, and
  `/revoke` rotates `api_key` too as defense in depth. Separately, a
  self-registered Automator/Admin who never went through the admin-run
  `create_user` CLI had no way to ever actually see their own key — a new
  self-service `POST /automators/<id>/regenerate-api-key` route (strictly
  self-only, `403` otherwise) shows a freshly generated key once via flash,
  the same one-time-display convention `create_user` already used.

## Reporting a Vulnerability
Report directly to the project owner. No name or contact is recorded in this repo
yet [placeholder — same unresolved gap as `CODE_OF_CONDUCT.md`'s enforcement
contact]. `SUPPORT.md`'s Telegram channel is also not a real fallback yet — it's
explicitly still a placeholder there too, and this repo only has one Telegram
bot/token defined, so whatever gets stood up there is likely the same bot
infrastructure as the Telegram limitation above — inheriting that access-control
gap rather than being a cleaner alternative.

Until an owner contact exists, use this repo's GitHub Security Advisories
("Report a vulnerability" under the Security tab) *if* private vulnerability
reporting has been turned on for this repo — that's a per-repo GitHub setting
this file (or anything else in the repo) can't confirm from outside GitHub's UI.
If the Security tab doesn't show that option, it hasn't been enabled yet — ask
the owner to turn it on. If you can't reach the owner and GHSA isn't available
either, the least-bad remaining option is a plain GitHub issue, kept to "there's
a vulnerability in `<component>`, contact me for details" with no exploit
specifics or repro steps in the issue body itself, asking the owner to move the
conversation to a private channel from there. That issue is visible to anyone
with repo access, so it's a last resort when the two options above genuinely
aren't available, not a first choice.

When reporting, include what component is affected (login/registration, the
Telegram bot, the API-key sync endpoint, or GitHub-sync — see Scope above),
repro steps, and impact — there's no formal severity rubric yet, so
when in doubt, report it and let the owner triage. (Repro steps and impact go
in the GHSA/owner-contact report itself, not in a public fallback issue — see
above.)

No fixed response-time commitment exists yet. Reporting or testing this app in
good faith won't be treated as an attack.
