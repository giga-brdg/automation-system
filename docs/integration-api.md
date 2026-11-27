# Read-only integration API (Supplax Dashboard)

`src/integration_api.py` — the JSON the Supplax Dashboard reads to show this portfolio inside its own shell (its users,
rights and design system). It **only serialises** what this app already computes for its HTML pages:
`value_dashboard.build_context` / `build_row`, `metrics_sync` and the model properties. No formula lives in Supplax.

## Switching it on

| Variable | Where | Meaning |
|---|---|---|
| `INTEGRATION_API_TOKEN` | service `web` | ≥ 32 random chars. Unset / shorter → every route answers `503 integration_disabled`. Generate: `python -c "import secrets; print(secrets.token_hex(32))"`. The **same value** goes to Supplax as `INTEGRATION_AUTOMATION_TOKEN`. |

Nothing else changes: no new table, no migration, no new service or cron, no change to the GitHub sync, the
Telegram bot or `token-usage-cron`. The HTML pages, sessions, CSRF and the existing `/api/*` (X-API-Key) are untouched.

## Rules

* `GET` only, `Authorization: Bearer <INTEGRATION_API_TOKEN>`, compared with `hmac.compare_digest`; a user `api_key`
  is **not** accepted (401). CSRF-exempt (no session). `Cache-Control: no-store`.
* Never a password hash, `api_key`, registration code or GitHub login in any answer. An **e-mail only in `/people`**
  (the work e-mail: Supplax links your people to its own users with it, on its server, admins only).
* Read-only: no route writes to the DB, calls GitHub / Telegram / the security-scan workflow or starts a sync.
  (`/automation` reads the external ai-usage-collector DB exactly like the HTML detail page does, read-only.)
* `None` stays `null` (never `0`); enum values are the lowercase API values (`live`, `admin`), not the DB names.
* Next to every figure the page prints there is the page's own text (`fmt_usd`, `fmt_hours`, `fmt_number`,
  `fmt_date`): Python rounds half to even and uses U+2212 / U+00A0 — a consumer must show these texts, not re-round.

## Envelope (every answer)

```json
{ "api_version": "1.0", "contract": "automation.value", "request_id": "<X-Request-Id if safe, else uuid4>",
  "generated_at": "2026-11-25T10:00:00Z", "data_as_of": "<newest metrics snapshot fetched_at, else newest automation.updated_at>",
  "freshness": { "status": "fresh|stale|no_data", "stale_after_hours": 36 }, "period": null, "...payload": "…" }
```

`stale` = the newest metrics snapshot is older than 36 h (the org sync runs daily at 03:00 UTC).
Errors: `{"error": {"code", "message"}}` — 400 `invalid_params`, 401 `unauthorized`, 404 `not_found`, 503 `integration_disabled`.

## Resources (`/api/integration/v1/automation/…`)

| Path | Contract | Query | Mirrors | Who sees it in Supplax |
|---|---|---|---|---|
| `value` | `automation.value` | `status` (5 statuses \| `all`, default `live`), `department`, `owner`, `only`, `today` (YYYY-MM-DD, tests only) | `/value` | «automation» permission |
| `automations` | `automation.registry` | `status`, `department`, `q`, `only` | `/automations` | «automation» permission |
| `pending` | `automation.pending` | — | the «found in GitHub, not ready» block | Supplax admins |
| `automation` | `automation.detail` | `slug` (required) | `/automations/<slug>` | «automation» permission |
| `automator` | `automation.automator` | `id` (required) | `/automators/<id>` (no e-mail) | «automation» permission |
| `skills` | `automation.skills` | — | `/skills` | «automation» permission |
| `departments` | `automation.departments` | — | `/departments` | Supplax admins |
| `subscriptions` | `automation.subscriptions` | — | `/subscriptions` | Supplax admins |
| `people` | `automation.people` | — | users: id, name, initials, role, **work e-mail**, approved, owned slugs | Supplax server only (never a browser) |

An invalid `status` is a 400 here (the HTML `/automations` page would raise a 500).

**`only=<slug,slug,…>`** (value, automations): restricts the answer to those automations — rows, every total, the
filter options, the subscriptions list and the registry counts are computed over that set by the same code. Present
but empty (`only=`) = nothing. ≤ 1000 slugs. Supplax sends the viewer's allowed set (an automator sees only their own
automations unless a Supplax admin opened more); without `only` the answer is the whole portfolio, as before.
The full field-by-field contract is in the Supplax integration package (`CONTRACT.md`).

## Tests

`tests/test_integration_api.py` (41 tests): auth / disabled / GET only, envelope, numbers == `build_context`, money
buckets and texts, old format, filters, empty portfolio keeps `null`, every resource, bad params, no secrets / e-mails (outside `/people`), `only` recomputes totals, `/people`,
read-only (row counts and `updated_at` unchanged), HTML pages still need a login.

`tools/export_integration_fixtures.py <dir>` writes SYNTHETIC answers (all values «ТЕСТ») of every resource — the
contract fixtures of the Supplax smoke tests. Re-run it after any contract change.
