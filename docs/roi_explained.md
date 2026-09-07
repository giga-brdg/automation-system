# ROI — How This Is Actually Calculated

The detailed companion to `dashboard/ROI.md` — the real walkthrough behind its terse,
dashboard-parsed fields. This describes the mechanism the dashboard app applies to
every automation's ROI card, including this repo's own — it isn't a portfolio-wide
standard other automations' repos read; each repo keeps its own `docs/roi_explained.md`
and its own `dashboard/ROI.md` generated from it. Most of what's below (the
`github_sync.py`/`app.py` traces in Caveats and Confidence) documents this repo's
own dashboard codebase specifically, since that's what lives here — another
automation's `docs/roi_explained.md` won't have an equivalent implementation trace
to write, just its own hypothesis and methodology for its own ROI number.

## Value Hypothesis

The Estimated hypothesis below isn't just an adjective — it rests on two concrete
mechanisms, each with real numbers behind it as of this writing.

**Quality.** Before this dashboard, an automation's actual status lived only in scattered
places — its own GitHub repo, a ClickUp task, or whoever happened to remember a Slack
conversation about it. `dashboard/SUMMARY.md`'s own "What it does" names the real
failure mode this causes: without one shared list, automations "дублюються, губляться
або просто через рік ніхто не пам'ятає, для чого їх колись зробили." The Виміряно/Оцінка
split (see Caveats) is a structural quality mechanism, not just a label: `github_sync.py`
only ever flips a card's confidence to "measured" when its Confidence section's text
literally starts with that word, so a plain hypothesis can't silently pass itself off as
a verified result. How many cards in the live portfolio actually say "Виміряно" today is
a fact about the production dashboard's real data, not this repo's code — read it off the
dashboard itself rather than trusting a count written into this file, since that number
moves every time an automation registers or re-syncs (and this repo's own local dev
database is seed data for testing, not a stand-in for that live count).

**Time.** The GitHub-pull sync (`/automations/<slug>/resync`, the "Оновити з GitHub"
button, and the initial `/automations/import-github` import) mechanically replaces a
manual step: each call reads structured fields out of 6 separate files in the target
repo (`README.md`, `dashboard/ROI.md`, `dashboard/SUMMARY.md`, `dashboard/functions.md`,
`dashboard/TODO.md`, `backlog/BACKLOG.md` — see `README.md`'s own description of what
`import-github` reads) that someone would otherwise open one at a time and copy into a
tracker by hand. That six-file count is a fact about the documented import behavior
itself, not something that depends on how many automations happen to be registered right
now. Putting the manual version of that pass at roughly 10-15 minutes per automation per
update (open 6 files, find the fields, retype them somewhere) is a stated, reasonable
ESTIMATE, not a measured one — same Confidence as everything else here — but it's the
actual arithmetic behind "this saves time," not just a claim. That per-sync saving
doesn't get more expensive as the portfolio grows; hunting the same information by hand
across GitHub, ClickUp, and chat does.

## Methodology
Per-automation ROI card shows time and/or money saved vs. the manual process it
replaced, plus an optional qualitative-value list and an optional presentation link
(see "What Else Is On the Card" below). That's the intended headline metric, not an
enforced one: `measured_value` is a free-text field with no unit check, so a card can
end up showing something else entirely — the one real "measured" example in this repo
(see Caveats) is a forecast-accuracy percentage, not a time or money figure. The
time/money baseline comes from whatever
the owner writes into the registration form's free-text `Гіпотеза` / `Як вимірюємо`
fields (e.g. "this used to take 3h/week manually") — there's no independent
measurement of the pre-automation process, and no structured schema (no dedicated
"hours/week" number field) behind that text yet. So a capture mechanism already
exists today, just an informal one: a generic hypothesis textbox, not a purpose-built
baseline field. Whether that gets replaced by something structured — a dedicated
hours-per-week input, a periodic owner check-in, or something else — is still a
design-stage decision. Later phases add token cost per automation (daily) as a
second, ongoing cost side of the same ROI picture — not implemented yet (no
`token_cost` field exists on `ROIEntry`).

### What else is on the card
Five `dashboard/ROI.md` sections feed `ROIEntry`. One of them — Actual Results —
supplies the headline number itself (see Methodology above); the other four add
context around it:
- **Hypothesis** → `hypothesis`, the free-text baseline this same Methodology
  section above is built around (the registration form's `Гіпотеза` field, e.g.
  "this used to take 3h/week manually"). Rendered as the `roi-baseline` line under
  the divider.
- **How We'll Measure It** → `metric_description`, the registration form's `Як
  вимірюємо` field. Rendered as the `roi-caption` line directly under the headline
  number.
- **Qualitative Value** → `qualitative_notes`, shown as a bullet list under the ROI
  number. The parser only picks up lines starting with `-` or `*`
  (`github_sync.py`'s `_bullet_items`/`_BULLET_RE`) — plain prose written under that
  heading without a bullet prefix is silently dropped (no error, nothing renders).
  So despite there being no format *requirement*, there is a format the section must
  follow to survive sync: bullet points.
- **Actual Results** → `measured_value`. Two separate things are true about it,
  on two separate sides of the sync, and they're easy to conflate:
  - **Render side (unconditional):** once a value is stored, the template
    shows it as the big number regardless of confidence — it doesn't check
    confidence before rendering (see Caveats: the "Виміряно"/"Оцінка" badge and
    the number below it are independent renders, not a gate).
  - **Write side (gated):** `roi_fields_from_sections` only reads Actual
    Results into `measured_value` *at all* when Confidence's own text starts
    with "measured" (`github_sync.py`, `confidence == "measured"`). If
    Confidence stays "Estimated" — the repo's own current state — anything
    written under Actual Results is ignored in full on that sync, silently,
    with no warning. This is a materially different trap than "it gets
    truncated": a stranger who fills in Actual Results before remembering to
    flip Confidence's wording will see nothing appear post-sync and have no
    on-screen indication why. When the gate does pass, the extraction keeps
    only the first non-blank line of Actual Results and stops — any additional
    lines are silently dropped, the same kind of gotcha as Qualitative Value's
    bullet-only parsing above.
- **Presentation** → `presentation_url`, an optional link to a slide-deck Artifact,
  rendered as a button when present. This repo's own `dashboard/ROI.md` doesn't
  currently have a `## Presentation` section (it's an opt-in section other repos'
  entries add when they have a deck) — the field and its rendering exist and are
  wired end-to-end, just unused here today.

All of this depends on `dashboard/ROI.md`'s section headers matching the expected
text exactly. `parse_markdown_sections` (`github_sync.py`) keys sections by the
literal `## Heading` text with no fuzzy matching, normalization, or error on a
near-miss — `## Actual results` or `## Result` instead of `## Actual Results`
silently yields nothing for that field on sync, with no warning that the heading
wasn't recognized. This is likely the single easiest mistake to make when hand-
editing `ROI.md`, and it isn't specific to Actual Results — it applies to all six
headings the same way.

Four of these five sync one-directional and additive-only through the
GitHub-sync path — reached via the browser's `/automations/<slug>/resync`
("Оновити з GitHub") button or the initial `/automations/import-github` import,
both of which call `sync_automation_from_github` — whose ROI block applies
`fields[x] or automation.roi.X` for `hypothesis`, `metric_description`,
`measured_value`, `presentation_url`, and `qualitative_notes`, so an
empty/removed section in `ROI.md` never clears one of *these* fields on the next
sync. **Confidence is the exception**: `app.py` sets
`automation.roi.confidence = fields["confidence"]` unconditionally, with no `or`
fallback. `roi_fields_from_sections` defaults confidence to `"estimated"`
whenever the Confidence section is missing or doesn't start with "measured", so
GitHub sync *can* and does reset a previously "measured" card back to
"estimated" — the one ROI column this additive-only guarantee does not cover.

There's a second, separate write path with different behavior: `/api/automations/
<slug>/sync`, the API-key-authenticated route stage-0-supplax's portfolio-sync step
calls after a build finishes. Its `roi` block uses `roi.get(key,
automation.roi.key)` — a missing-*key* fallback, not falsy-or — so a caller that
sends an explicit empty string for `hypothesis`, `metric_description`,
`confidence`, `measured_value`, or `presentation_url` in that endpoint's JSON
payload *does* clear the field. `qualitative_notes` isn't in that route's `roi`
block at all, so it can't be cleared through it either.

Net effect on retraction: `qualitative_notes` currently has no UI or API path that
clears it — not the registration form (no field for it), not the GitHub-sync path
(additive-only), not the API route (not handled there) — so editing the database is
the only way to blank a wrong qualitative note today. Three fields (`hypothesis`,
`metric_description`, `presentation_url`) can be retracted through the
registration form, since it overwrites them on every save including with a blank
value; `measured_value` has no form field to retract it through, but all four
(plus `confidence`, see below) can be cleared via the API route's
explicit-empty-string path. Rewriting `ROI.md` back to blank and re-syncing
through the GitHub-sync path will not clear any of these four fields — that path
is additive-only for them.

`confidence` doesn't fit the pattern above and shouldn't be lumped in with the
other four's "blank value" retraction: the registration form renders it as a
two-option `<select>` (Оцінка / Виміряно, `automation_form.html`) with no blank
option, so a user can never submit an empty confidence through that form —
"retracting" it there means flipping the dropdown back to Estimated, not
blanking a field. `apply_manual_form` also defaults it via
`form.get("confidence", "estimated")`, so a malformed POST that omits the field
entirely silently downgrades a "measured" badge to "estimated" — a distinct risk
from the blank-value retraction the other three free-text fields have. And
unlike those four, confidence is *not* additive-only through GitHub sync (see
"What else is on the card" above): re-syncing a `ROI.md` whose Confidence section
was removed or reworded resets the stored value to "estimated" outright.

## Assumptions
- The owner's self-reported "time it used to take manually" is accurate enough to use
  as a baseline — it isn't independently verified against, say, old timesheets.
- An automation's ROI stays roughly constant once registered; nothing here re-checks
  whether the manual-process baseline is still realistic months later.

## Caveats
Most automations' ROI is a stated hypothesis, not an independently measured result,
and that's expected to stay the norm for a while — nothing here verifies a
self-reported baseline. But "no real numbers exist anywhere" isn't quite true today:
`ROIEntry` already supports `confidence="measured"` with a real value attached (the
`seed-demo` CLI command ships the Stage 0 sales-forecast automation with
`confidence="measured"`, `measured_value="9.8% MAPE"`, and its card renders a
"Виміряно" badge, not "Оцінка"). The registration form lets any owner flip the
Confidence dropdown to "Виміряно" directly — but the form has no `measured_value`
field at all (`automation_form.html` doesn't render one, and `apply_manual_form` in
`app.py` never reads or sets it), so an owner can only claim the badge through the UI,
not attach a number to it. The number itself can currently arrive via GitHub sync
(`github_sync.py`'s `roi_fields_from_sections`, reached through the browser's
`/automations/<slug>/resync` "Оновити з GitHub" button or the initial
`/automations/import-github` import), the API-key-authenticated
`/api/automations/<slug>/sync` route that stage-0-supplax's portfolio-sync step
calls (see "What else is on the card" above — its overwrite semantics differ from
GitHub sync's additive-only ones), or the `seed-demo` CLI. Nothing enforces the
badge/number pairing either way, though: `github_sync.py` flips confidence to "measured" purely by checking
whether the Confidence line's text starts with "measured" — it never checks that
Actual Results has any content. So a "Виміряно" badge means "someone wrote that
word," not "this number was independently checked." That's the real, ongoing bias
risk: an owner motivated to show their automation's value has no independent check
on the number they enter, whether the card says Estimated or Measured.

That "measured" check is also an English-keyword requirement, which sits at odds
with this repo's own convention for the file it's checking: `dashboard/ROI.md`'s
template comment tells authors to write the file "in Ukrainian on purpose", but
`github_sync.py`'s confidence check only recognizes a Confidence section whose
text literally starts with the English word "measured" (case-insensitive). A
fully Ukrainian phrasing like "Виміряно" would never flip confidence — the
current file only works because its own Confidence line happens to start with
the English word "Estimated" rather than a Ukrainian equivalent. This is a real
trap for exactly the Ukrainian-authoring workflow the template asks for, not
just the abstract self-report bias noted above.

The same unenforced coupling also breaks in the opposite direction on re-sync: the
GitHub-sync handler always takes the new `confidence` from `ROI.md` outright, but
falls back to the *old* stored `measured_value` whenever the new sync's value is
empty (see "What else is on the card" above). So an owner who edits `ROI.md` to move
Confidence back from Measured to Estimated — retracting a claim — gets a card whose
badge correctly flips to "Оцінка," while the big number keeps showing the stale
"measured" value from the previous sync, because the number display itself
(`automation_detail.html`'s `roi-number`) doesn't check confidence at all.

## Confidence — Why
Estimated by default, and this repo's own `dashboard/ROI.md` entry stays Estimated
until a structured capture mechanism gets built and a real post-launch number
replaces today's free-text estimate. That "until" is an editorial commitment this
repo's maintainer is choosing to keep, not a technical gate — nothing in the code
actually stops anyone from editing this repo's `ROI.md` to say "Measured" today, with
or without a structured mechanism behind it (see Caveats' unenforced-gate point).
And even honored, that transition only fixes "this card has no number yet" — it
doesn't touch the self-report bias named in Caveats. A "measured" number typed by
the automation's own owner is still unverified, and stays unverified after Estimated
flips to Measured.

## Loose ends
- **`measured_at`** — `ROIEntry` has a `measured_at` timestamp column
  (`models.py`), but nothing in the codebase ever sets or reads it (no sync path,
  form field, or template references it). It's schema, not a live mechanism; don't
  assume a measurement date is tracked anywhere today.
