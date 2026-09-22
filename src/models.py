import enum
import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def _now():
    return datetime.now(timezone.utc)


class Role(enum.Enum):
    ADMIN = "admin"
    AUTOMATOR = "automator"
    VIEWER = "viewer"

    @property
    def label(self):
        return {
            Role.ADMIN: "Адміністратор",
            Role.AUTOMATOR: "Автоматизатор",
            Role.VIEWER: "Глядач",
        }[self]


class Status(enum.Enum):
    IDEA = "idea"
    IN_DEVELOPMENT = "in_development"
    READY_NOT_LAUNCHED = "ready_not_launched"
    LIVE = "live"
    ARCHIVED = "archived"

    @property
    def label(self):
        return {
            Status.IDEA: "Ідея",
            Status.IN_DEVELOPMENT: "У розробці",
            Status.READY_NOT_LAUNCHED: "Готово, не запущено",
            Status.LIVE: "Працює",
            Status.ARCHIVED: "Зупинено / Архів",
        }[self]

    @property
    def bs_variant(self):
        """Bootstrap semantic variant for the status badge (bg-light-{variant})."""
        return {
            Status.IDEA: "secondary",
            Status.IN_DEVELOPMENT: "warning",
            Status.READY_NOT_LAUNCHED: "primary",
            Status.LIVE: "success",
            Status.ARCHIVED: "danger",
        }[self]


def hue_for(name):
    """Deterministic hue (0-359) for a category/department name, same
    approach the reference design uses: hash the name, don't hand-pick a
    color per row so a new department never needs a design decision."""
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) % 360
    return h


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.Enum(Role), nullable=False, default=Role.VIEWER)
    # Per-automator token so a skill can push automation data on their behalf
    # (see the "register-automation" skill this is meant to support) without
    # sharing a browser login/password. Never displayed after generation -
    # only regenerated, same as CLICKUP_API_TOKEN's own security posture.
    api_key = db.Column(db.String(64), unique=True, nullable=False, default=lambda: secrets.token_hex(32))
    created_at = db.Column(db.DateTime, default=_now)
    # Self-service registration gate (see /register, /confirm, src/telegram_bot.py).
    # is_confirmed: the person entered the code the admin relayed to them out
    # of band - proves they're a real person the admin let in, nothing more.
    # is_approved: the admin actually granted a role via the "/grant" bot
    # command. Login requires BOTH - a confirmed-but-unapproved account is a
    # verified identity with zero access, by design. Accounts made via the
    # `create-user` CLI (an admin typing directly, already trusted) set both
    # True immediately and never go through this flow.
    is_confirmed = db.Column(db.Boolean, nullable=False, default=False)
    is_approved = db.Column(db.Boolean, nullable=False, default=False)
    pending_code = db.Column(db.String(10))
    pending_code_expires_at = db.Column(db.DateTime)
    # Brute-force guard on /confirm: the code is a 6-digit number with no
    # other rate limiting anywhere in the app, so this counts wrong guesses
    # and forces a fresh registration (a new code) past a threshold, rather
    # than leaving the same code guessable for its whole 30-minute window.
    # Reset to 0 whenever a new pending_code is issued or one is consumed.
    pending_code_attempts = db.Column(db.Integer, nullable=False, default=0)

    automations = db.relationship("Automation", back_populates="owner")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == Role.ADMIN

    @property
    def is_automator(self):
        return self.role == Role.AUTOMATOR

    def can_manage(self, automation):
        """Admin manages everything; an Automator only the automations they
        own (automation.owner_id == their own id); a Viewer manages none."""
        return self.is_admin or (self.is_automator and automation.owner_id == self.id)

    @property
    def initials(self):
        parts = self.name.split()
        letters = "".join(p[0] for p in parts[:2] if p)
        return letters.upper() or "?"


automation_departments = db.Table(
    "automation_departments",
    db.Column("automation_id", db.Integer, db.ForeignKey("automation.id"), primary_key=True),
    db.Column("department_id", db.Integer, db.ForeignKey("department.id"), primary_key=True),
)


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    hue = db.Column(db.Integer, nullable=False, default=0)  # 0-359, oklch() hue for this department's pill

    @property
    def pill_style(self):
        return f"--dept-pill-bg: oklch(93% 0.03 {self.hue}); --dept-pill-fg: oklch(35% 0.09 {self.hue});"

    @property
    def pill_style_active(self):
        """Same hue formula as pill_style, solid instead of tinted - for a
        pressed/selected filter pill. Kept as a second property (not derived
        in the template) so there's one source of truth for the hue math."""
        return f"--dept-pill-bg: oklch(35% 0.09 {self.hue}); --dept-pill-fg: #fff;"


automation_subscriptions = db.Table(
    "automation_subscriptions",
    db.Column("automation_id", db.Integer, db.ForeignKey("automation.id"), primary_key=True),
    db.Column("subscription_id", db.Integer, db.ForeignKey("subscription.id"), primary_key=True),
)


class Subscription(db.Model):
    """A shared recurring cost - a Claude Code seat, a VPS, Railway, a domain -
    unlike ai_usage.py's token spend, there's no external billing API this can
    pull from, so monthly_cost_usd is a plain number someone types in and keeps
    current by hand. Deliberately its own entity rather than a field on
    Automation: the same VPS or Claude Code seat is often shared by several
    automations at once (see cost_per_automation below), the same reason
    Department is its own table instead of a string column."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    provider = db.Column(db.String(255))
    monthly_cost_usd = db.Column(db.Numeric(10, 2), nullable=False)
    notes = db.Column(db.Text)

    automations = db.relationship("Automation", secondary=automation_subscriptions, backref="subscriptions")

    @property
    def cost_per_automation(self):
        """An explicit equal split across every linked automation, not a
        guessed weight - visible and auditable, same honesty rule ROI's own
        numbers already follow. None (not zero) when nothing is linked yet,
        so a template can tell "unallocated" apart from "free"."""
        if not self.automations:
            return None
        return self.monthly_cost_usd / len(self.automations)


automation_skills = db.Table(
    "automation_skills",
    db.Column("automation_id", db.Integer, db.ForeignKey("automation.id"), primary_key=True),
    db.Column("skill_id", db.Integer, db.ForeignKey("skill.id"), primary_key=True),
)


class SecurityReviewMixin:
    """Shared by Automation and Skill: a record of the last time someone ran
    Claude Code's built-in `/security-review` against that repo's pending
    changes, synced from a dashboard/SECURITY_REVIEW.md file the repo owner
    writes by hand after a real run - not a full-codebase audit, and not
    something this dashboard can trigger itself. `security_review_at` stays
    None until such a file actually gets synced - "never reviewed" is a
    real, expected state the badge shows honestly rather than defaulting to
    clean. See SECURITY.md's Known Limitations for why this is a
    point-in-time attestation, not a live guarantee: nothing re-checks it as
    the repo keeps changing underneath it."""
    security_review_at = db.Column(db.DateTime)
    security_review_high = db.Column(db.Integer)
    security_review_medium = db.Column(db.Integer)

    _SECURITY_REVIEW_LABELS = {
        "none": "Не перевірено",
        "clean": "Перевірено — чисто",
        "medium": "Перевірено — є знахідки (Medium)",
        "high": "Перевірено — є знахідки (High)",
    }

    @property
    def security_review_state(self):
        """"none" / "clean" / "medium" / "high" - severity of the worst
        still-open finding, not just "reviewed vs not". Drives both the
        shield chip's color and its shape (src/templates/_security_chip.html)."""
        if self.security_review_at is None:
            return "none"
        if (self.security_review_high or 0) > 0:
            return "high"
        if (self.security_review_medium or 0) > 0:
            return "medium"
        return "clean"

    @property
    def security_review_label(self):
        return self._SECURITY_REVIEW_LABELS[self.security_review_state]


class Automation(SecurityReviewMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    one_liner = db.Column(db.String(500))  # short teaser, shown on the card in the registry grid
    description = db.Column(db.Text)  # full "what it does / goal" writeup, shown on the detail page
    status = db.Column(db.Enum(Status), nullable=False, default=Status.IDEA)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    repo_url = db.Column(db.String(500))
    clickup_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)
    # Manual "+"-style link to ai-usage-collector's own projects.id (a
    # different system's Postgres - see src/ai_usage.py). No cross-DB FK is
    # possible; this is a plain nullable int the automator looks up and types
    # in themselves. Null means "not tracked here" - the token-usage panel
    # and check-token-usage both skip the automation silently, not an error.
    ai_usage_project_id = db.Column(db.Integer)
    # Manually-set monthly USD budget - OpenAI's Usage API has no "remaining
    # balance" concept, so this is the dashboard's own comparison point, not
    # something read from OpenAI.
    monthly_token_budget_usd = db.Column(db.Numeric(10, 2))
    # Per-automation override of ai_usage.SPIKE_MULTIPLIER_DEFAULT. Null uses
    # the default.
    token_spike_multiplier = db.Column(db.Numeric(4, 1))
    # De-dup state for check-token-usage so the same threshold-cross doesn't
    # re-alert every cron run. One of "budget_80"/"budget_100"/"spike"/null.
    last_token_alert_kind = db.Column(db.String(20))
    last_token_alert_at = db.Column(db.DateTime)
    # Answers to stage-1-supplax's 9-phase interview (src/stage1_questions.py),
    # filled in the dashboard at registration time instead of live in a Claude
    # Code session - {"1": {"one_liner": "...", ...}, "2": {...}, ...}, phase
    # numbers/question keys matching that skill's own reference file 1:1. Read
    # by GET /api/automations/<slug>/stage1-answers so a bootstrap run can
    # treat these as already-known and only ask about what's actually missing.
    # Null/empty means nobody has filled this in yet - not the same as "every
    # answer was blank".
    stage1_answers = db.Column(db.JSON)

    owner = db.relationship("User", back_populates="automations")
    departments = db.relationship("Department", secondary=automation_departments, backref="automations")
    skills = db.relationship("Skill", secondary=automation_skills, backref="automations")
    roi = db.relationship("ROIEntry", back_populates="automation", uselist=False, cascade="all, delete-orphan")
    comparison = db.relationship("Comparison", back_populates="automation", uselist=False, cascade="all, delete-orphan")
    review_log = db.relationship(
        "ReviewLogEntry", back_populates="automation", cascade="all, delete-orphan",
        order_by="ReviewLogEntry.order_index.desc()",
    )
    connections = db.relationship(
        "Connection", foreign_keys="Connection.automation_id",
        back_populates="automation", cascade="all, delete-orphan",
    )
    pages = db.relationship(
        "AutomationPage", back_populates="automation", cascade="all, delete-orphan",
        order_by="AutomationPage.order_index",
    )
    todo_items = db.relationship(
        "AutomationTodoItem", back_populates="automation", cascade="all, delete-orphan",
        order_by="AutomationTodoItem.order_index",
    )

    @property
    def monthly_subscription_cost_usd(self):
        """Sum of this automation's equal share of every Subscription it's
        linked to (see Subscription.cost_per_automation) - None rather than 0
        when it has no subscriptions linked, so a template can tell
        "genuinely free" apart from "nobody's recorded this yet" the same way
        ROIEntry's own None-vs-0 fields already do."""
        shares = [s.cost_per_automation for s in self.subscriptions if s.cost_per_automation is not None]
        return sum(shares) if shares else None


class ROIEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=False)
    hypothesis = db.Column(db.Text)
    metric_description = db.Column(db.Text)
    confidence = db.Column(db.String(20), default="estimated")  # estimated | measured
    measured_value = db.Column(db.Text)
    measured_at = db.Column(db.DateTime)
    qualitative_notes = db.Column(db.Text)
    # Link to a published HTML slide-deck Artifact walking through this ROI
    # entry - from dashboard/ROI.md's '## Presentation' section (optional, most
    # automations won't have one). Opens in a new tab; not embedded, since
    # Artifacts are served from claude.ai and can't be iframed here.
    presentation_url = db.Column(db.String(500))

    # Structured time metrics - filled from Stage 1's Phase 1 (src/stage1_questions.py),
    # pre-filled there from AUTOMATION_REQUEST.md's own cycle x frequency arithmetic
    # when a Stage 0 brief exists. Kept as the raw cycle/frequency inputs rather than
    # just a final hours-per-month total, so the number stays auditable - see the
    # *_hours_per_month properties below for the derived figures. Every other ROI
    # metric type (conversion/quality/cost) stays free text in metric_description/
    # measured_value above - only time shares a common unit across every automation,
    # so only time is safe to sum across the whole portfolio.
    baseline_cycle_minutes = db.Column(db.Numeric(10, 2))
    baseline_frequency_per_month = db.Column(db.Numeric(10, 2))
    target_cycle_minutes = db.Column(db.Numeric(10, 2))
    target_frequency_per_month = db.Column(db.Numeric(10, 2))
    # Filled in later, once the automation has actually run a while and someone
    # re-checks the real number - deliberately separate from target_hours_per_month
    # (the estimate made at intake time), the same "Estimated vs Measured" distinction
    # `confidence` already draws, just with a real figure behind it now instead of
    # only a free-text claim in measured_value.
    measured_hours_per_month = db.Column(db.Numeric(10, 2))

    # Cost side, same honesty rule as the benefit side above: real numbers or
    # nothing, never a guess. dev_hours is one-time (building v1) - kept
    # separate from maintenance_hours_per_month (ongoing, re-editable current
    # figure, same "current value not a log" shape as measured_hours_per_month)
    # on purpose, so a one-time cost never gets silently treated as recurring
    # in net_hours_per_month below. Neither converts to money - there's no
    # portfolio-wide hourly rate anywhere in this codebase, and inventing one
    # would misrepresent every automation's real number, the same trap
    # dashboard/ROI.md's own template already warns against.
    dev_hours = db.Column(db.Numeric(10, 2))
    maintenance_hours_per_month = db.Column(db.Numeric(10, 2))

    automation = db.relationship("Automation", back_populates="roi")

    @staticmethod
    def _hours_per_month(cycle_minutes, frequency_per_month):
        if cycle_minutes is None or frequency_per_month is None:
            return None
        return cycle_minutes * frequency_per_month / 60

    @property
    def baseline_hours_per_month(self):
        return self._hours_per_month(self.baseline_cycle_minutes, self.baseline_frequency_per_month)

    @property
    def target_hours_per_month(self):
        # Frequency rarely changes just because a process got automated - fall back
        # to the baseline frequency if the automator left target_frequency blank.
        frequency = self.target_frequency_per_month
        if frequency is None:
            frequency = self.baseline_frequency_per_month
        return self._hours_per_month(self.target_cycle_minutes, frequency)

    @property
    def estimated_hours_saved_per_month(self):
        baseline, target = self.baseline_hours_per_month, self.target_hours_per_month
        if baseline is None or target is None:
            return None
        return baseline - target

    @property
    def best_hours_saved_per_month(self):
        """measured_hours_per_month when a real post-launch number exists,
        else the Phase 1 estimate - same Measured-beats-Estimated hierarchy
        `confidence` already draws elsewhere on this model."""
        if self.measured_hours_per_month is not None:
            return self.measured_hours_per_month
        return self.estimated_hours_saved_per_month

    @property
    def net_hours_per_month(self):
        """best_hours_saved_per_month minus the ongoing maintenance cost, both
        already hours/month - None unless both sides are known. Deliberately
        excludes dev_hours: that's a one-time cost, not a monthly one, so
        folding it into this figure would misrepresent it as recurring."""
        saved = self.best_hours_saved_per_month
        if saved is None or self.maintenance_hours_per_month is None:
            return None
        return saved - self.maintenance_hours_per_month


class Comparison(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=False)
    old_way_description = db.Column(db.Text)
    limitations = db.Column(db.Text)

    automation = db.relationship("Automation", back_populates="comparison")
    features = db.relationship("FeatureRow", back_populates="comparison", cascade="all, delete-orphan")


class FeatureRow(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    comparison_id = db.Column(db.Integer, db.ForeignKey("comparison.id"), nullable=False)
    feature = db.Column(db.String(255))
    old_way = db.Column(db.String(255))
    new_way = db.Column(db.String(255))
    why_it_matters = db.Column(db.String(255))

    comparison = db.relationship("Comparison", back_populates="features")


class Connection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=False)
    connected_automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=False)
    relationship_type = db.Column(db.String(100))
    shared_resource = db.Column(db.String(255))

    automation = db.relationship("Automation", foreign_keys=[automation_id], back_populates="connections")
    connected_automation = db.relationship("Automation", foreign_keys=[connected_automation_id])


class ReviewLogEntry(db.Model):
    """Mirrors stage-1-supplax's backlog/BACKLOG.md entries, in the DB -
    parsed straight from that file by github_sync, not hand-entered."""
    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=False)
    round_label = db.Column(db.String(100))
    found = db.Column(db.Text)
    changed = db.Column(db.Text)
    rejected = db.Column(db.Text)
    # BACKLOG.md entries have no reliable timestamp to sort by (the <label>
    # is free-form per backlog-format.md) - this is just "file order",
    # reset and reassigned on every sync so newest-appended sorts first.
    order_index = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=_now)

    automation = db.relationship("Automation", back_populates="review_log")


class AutomationTodoItem(db.Model):
    """One `- [ ]`/`- [x]` line from the automation's own TODO.md - stage-0-
    supplax already creates this file per project (deliberately empty at
    bootstrap, "fills up from real work" per its own template comment); this
    just reads it, it doesn't ask automators to maintain anything new."""
    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    done = db.Column(db.Boolean, nullable=False, default=False)
    order_index = db.Column(db.Integer, nullable=False, default=0)

    automation = db.relationship("Automation", back_populates="todo_items")


class AutomationPage(db.Model):
    """One entry per screen/page of the automation, from dashboard/SUMMARY.md's
    '## Pages' section - see stage-1-supplax's templates/dashboard/SUMMARY.md.
    Plain-language only: this is what a non-technical viewer reads to
    understand what they'd actually click on and why."""
    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(db.Integer, db.ForeignKey("automation.id"), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    # Longer write-up of this same page/screen, from dashboard/functions.md
    # (a generated mirror of docs/functions.md - see automation-portfolio-sync's
    # SKILL.md). Optional: older repos synced before this field existed, or
    # ones without dashboard/functions.md, just leave it null.
    detail = db.Column(db.Text)
    order_index = db.Column(db.Integer, nullable=False, default=0)

    automation = db.relationship("Automation", back_populates="pages")


class Skill(SecurityReviewMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.Text)
    when_to_use = db.Column(db.Text)
    doc_url = db.Column(db.String(500))
    # Set only for skills pulled in via /skills/import-github (one repo = one
    # skill, its SKILL.md at repo root) - lets re-importing the same repo
    # update this row by name instead of creating a duplicate. Null for
    # skills that only ever arrived via the API sync payload or seed-demo.
    repo_url = db.Column(db.String(500))
    # security_review_at/high/medium (SecurityReviewMixin) come from
    # {path/}dashboard/SECURITY_REVIEW.md in the skill's own repo (or its
    # subdirectory, for a shared multi-skill repo) - read by
    # sync_skill_from_github / sync_skills_from_github_folder in src/app.py,
    # same file format Automation uses. Null for a skill with no repo_url at
    # all (added via the API payload or seed-demo) - there's nothing to sync
    # from, so "never reviewed" is simply correct, not a gap.


class PendingAutomation(db.Model):
    """A non-archived repo `sync-github-org` (src/app.py) found under the
    GitHub org that hasn't reached the dashboard yet, because it's missing
    dashboard/SUMMARY.md - the one file the real sync requires. Deliberately
    NOT an Automation row: it has no owner, no ROI, nothing a real
    automation needs - just "this exists, here's what's missing," so the
    gap is visible instead of the repo just silently not showing up.
    `missing` also notes whether stage-1-supplax's PIPELINE.md is present
    (bootstrapped, never finished reaching the dashboard) or not (nobody's
    confirmed this is even meant to be an automation) - but every
    non-archived repo without dashboard/SUMMARY.md gets a row either way,
    because that distinction alone isn't reliable enough to silently drop a
    repo on: a real, hand-rolled automation with no stage-0 history looks
    identical to an SDK package or a shared skills workspace from here. An
    automator dismisses whichever of those turn out not to be real
    automations - see `dismissed` below."""
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    repo_url = db.Column(db.String(500), nullable=False)
    # Free text naming what's missing, e.g. "dashboard/SUMMARY.md" - a single
    # known cause today, but written as text (not a fixed enum) since a
    # second cause is a plausible future addition and this isn't parsed
    # anywhere, only ever displayed.
    missing = db.Column(db.String(500), nullable=False)
    discovered_at = db.Column(db.DateTime, default=_now)
    last_seen_at = db.Column(db.DateTime, default=_now)
    # An automator can dismiss a false positive (a repo that has PIPELINE.md
    # for some unrelated reason but was never meant to become a tracked
    # automation) without it reappearing on every sync run - see
    # sync-github-org's upsert logic, which never clears this flag itself.
    dismissed = db.Column(db.Boolean, nullable=False, default=False)
