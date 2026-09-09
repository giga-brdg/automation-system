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
    def dot_color(self):
        """oklch() token name (see style.css :root) used for the status dot."""
        return {
            Status.IDEA: "var(--text-muted)",
            Status.IN_DEVELOPMENT: "var(--amber)",
            Status.READY_NOT_LAUNCHED: "var(--accent)",
            Status.LIVE: "var(--green)",
            Status.ARCHIVED: "var(--red)",
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
        return f"background: oklch(93% 0.03 {self.hue}); color: oklch(35% 0.09 {self.hue});"


automation_skills = db.Table(
    "automation_skills",
    db.Column("automation_id", db.Integer, db.ForeignKey("automation.id"), primary_key=True),
    db.Column("skill_id", db.Integer, db.ForeignKey("skill.id"), primary_key=True),
)


class Automation(db.Model):
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
    # Separate from updated_at, which also changes on a manual edit - this is
    # specifically "when did a GitHub pull last actually happen", so the page
    # can say which kind of freshness it's showing.
    last_synced_at = db.Column(db.DateTime)
    # Deliberately a raw fact (the last commit's own subject line + when it
    # happened), not an interpreted "stage" - a commit type/message doesn't
    # reliably map to lifecycle stage, and pretending otherwise misleads more
    # than it helps. See TODO.md's "real-time build stage" entry for why this
    # shape was chosen over a guessed one.
    last_commit_message = db.Column(db.String(500))
    last_commit_at = db.Column(db.DateTime)
    # Optional human override (dashboard/SUMMARY.md's '## Current Stage') for
    # anything a commit can't say - "очікуємо погодження", "заблоковано
    # тікетом X". Takes priority over last_commit_message when present.
    current_stage_override = db.Column(db.String(500))
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

    automation = db.relationship("Automation", back_populates="roi")


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
    """Mirrors stage-0-supplax's backlog/BACKLOG.md entries, in the DB -
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
    '## Pages' section - see stage-0-supplax's templates/dashboard/SUMMARY.md.
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


class Skill(db.Model):
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
