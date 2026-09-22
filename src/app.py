import io
import os
import re
import secrets
import threading
import zipfile
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import click
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_limiter import Limiter
from flask_limiter.errors import RateLimitExceeded
from flask_limiter.util import get_remote_address
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

from . import ai_usage, github_sync, telegram
from . import stage1_questions
from .extensions import db, login_manager
from .models import (
    Automation,
    AutomationPage,
    AutomationTodoItem,
    Comparison,
    Connection,
    Department,
    FeatureRow,
    PendingAutomation,
    ROIEntry,
    ReviewLogEntry,
    Role,
    Skill,
    Status,
    User,
    _now,
    hue_for,
)

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _safe_url(value):
    """Repo/ClickUp/presentation links get rendered straight into an
    href (see templates/automation_detail.html etc.) - drop anything not
    http(s) so a 'javascript:' URL entered via the edit form, the JSON
    sync API, or a synced repo's own dashboard/ROI.md can't run script in
    another user's session when they click the link."""
    value = (value or "").strip()
    if not value:
        return None
    return value if value.lower().startswith(("http://", "https://")) else None


# Module-level so register_routes (below) can reach it to exempt the one
# machine-facing, non-session endpoint (api_sync_automation) - CSRFProtect
# otherwise checks every POST/PUT/PATCH/DELETE, and that endpoint has no
# Flask session/cookie to carry a CSRF token in the first place.
csrf = CSRFProtect()

# In-memory storage (the default) is fine here - confirmed via the live
# Railway environment that `web` runs a single instance/replica, so there's
# no second process with its own separate counter to disagree with. Revisit
# (a shared store like Redis) only if that ever changes. No default_limits:
# every route is unlimited except /login, which opts in explicitly below -
# a blanket limit would also throttle things like the API-key sync endpoint
# that have no brute-forceable secret to protect in the same way.
limiter = Limiter(key_func=get_remote_address, default_limits=[], storage_uri="memory://")


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")

    default_db = f"sqlite:///{(BASE_DIR / 'data' / 'portfolio.db').as_posix()}"
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL") or default_db
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.environ.get("AUTH_SECRET") or "dev-only-insecure-key-set-AUTH_SECRET-in-.env"
    # HTTPONLY/SAMESITE are safe everywhere (including local http dev); SECURE
    # is tied to RAILWAY_ENVIRONMENT rather than hardcoded True so a plain
    # local `python -m src.app` over http still gets a cookie the browser
    # will actually send back - Railway sets this var in every deployed
    # environment (confirmed: RAILWAY_ENVIRONMENT=production today), so this
    # isn't guessing at how to detect "real deployment" vs. a laptop.
    # Named explicitly (not Flask's default "session") because this dashboard
    # is meant to end up sharing a domain/subdomain with other automations'
    # own apps - Flask-Login's default cookie name is generic enough that two
    # apps on the same host would silently overwrite each other's session
    # cookie in the browser's cookie jar. Every app landing on that shared
    # domain needs its own distinct name; this is this one's.
    app.config["SESSION_COOKIE_NAME"] = "automation_portfolio_session"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RAILWAY_ENVIRONMENT"))

    if os.environ.get("RAILWAY_ENVIRONMENT"):
        # Railway's own edge is the only reverse proxy in front of this app,
        # so trusting exactly one X-Forwarded-For hop is safe - without this,
        # request.remote_addr (what the rate limiter below keys on) sees only
        # Railway's proxy IP, and every visitor would share one bucket.
        # Skipped locally: nothing forwards a real client IP on a laptop, and
        # trusting the header there would let a request just claim any IP.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        flash("Форма застаріла або сесія скінчилась — спробуй ще раз.", "error")
        return redirect(request.referrer or url_for("index"))

    @app.errorhandler(RateLimitExceeded)
    def handle_rate_limit_error(e):
        flash("Забагато спроб входу — зачекай хвилину і спробуй ще раз.", "error")
        return render_template("login.html"), 429

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    register_routes(app)
    register_cli(app)
    return app


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def automator_required(view):
    """Admin or Automator - the two roles allowed to create automations at
    all. Which EXISTING automation a non-admin Automator may then edit/resync
    is a separate, per-automation check (User.can_manage) done inside the
    view once the automation is loaded, not something a route-level
    decorator can express."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in (Role.ADMIN, Role.AUTOMATOR):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def sync_automation_from_github(automation, repo_url, owner_id, form_status, selected_dept_ids, slug=None,
                                 prefetched_summary_text=None):
    """Shared by the first-time import form and the per-automation
    'Оновити з GitHub' button: fetch README.md/dashboard/ROI.md/
    dashboard/SUMMARY.md and apply them to `automation` (a new unsaved
    instance, or an existing one being refreshed). These two live in a
    dedicated dashboard/ folder because they're a generated sync
    contract, not hand-maintained project docs - the automation's repo
    keeps its own real ROI/functionality writeups elsewhere (docs/
    roi_explained.md, docs/functions.md) and regenerates these two from
    them. Raises on a GitHub fetch failure - callers turn that into a
    flash message.

    `prefetched_summary_text`: pass this when the caller already fetched
    dashboard/SUMMARY.md itself (run_github_org_sync does, to decide
    pending-vs-synced before calling here) so this doesn't fetch it again."""
    parsed = github_sync.parse_repo_url(repo_url)
    if not parsed:
        raise ValueError("Не схоже на посилання на GitHub-репозиторій "
                          "(очікую https://github.com/власник/репо).")
    owner_gh, repo = parsed

    branch = github_sync.default_branch(owner_gh, repo)
    readme_text = github_sync.fetch_raw_file(owner_gh, repo, "README.md", branch)
    roi_text = github_sync.fetch_raw_file(owner_gh, repo, "dashboard/ROI.md", branch)
    summary_text = (prefetched_summary_text if prefetched_summary_text is not None
                     else github_sync.fetch_raw_file(owner_gh, repo, "dashboard/SUMMARY.md", branch))
    functions_text = github_sync.fetch_raw_file(owner_gh, repo, "dashboard/functions.md", branch)
    backlog_text = github_sync.fetch_raw_file(owner_gh, repo, "backlog/BACKLOG.md", branch)
    security_review_text = github_sync.fetch_raw_file(owner_gh, repo, "dashboard/SECURITY_REVIEW.md", branch)
    # dashboard/TODO.md is the generated mirror automation-portfolio-sync
    # keeps in sync with the automation's real (root) TODO.md - prefer it,
    # but fall back to the root file for repos synced before that mirror
    # existed, so they don't silently lose their TODO section.
    todo_text = github_sync.fetch_raw_file(owner_gh, repo, "dashboard/TODO.md", branch)
    if todo_text is None:
        todo_text = github_sync.fetch_raw_file(owner_gh, repo, "TODO.md", branch)
    latest_commit = github_sync.fetch_latest_commit(owner_gh, repo, branch)

    title, one_liner = github_sync.parse_readme(readme_text)
    roi_sections = github_sync.parse_roi_md(roi_text)
    summary = github_sync.summary_fields_from_sections(
        github_sync.parse_markdown_sections(summary_text))

    if automation is None:
        automation = Automation(slug=slug or repo.lower(), owner_id=owner_id,
                                 name=summary["name"] or title or repo)
        db.session.add(automation)
    # dashboard/SUMMARY.md is the purpose-built contract - prefer it over
    # README's prose whenever it's actually present.
    automation.name = summary["name"] or title or automation.name
    automation.one_liner = summary["one_liner"] or one_liner or automation.one_liner
    automation.description = summary["description"] or automation.description
    automation.repo_url = repo_url
    automation.owner_id = owner_id
    automation.last_synced_at = _now()
    if summary_text:
        # Only overwrite the manual override when SUMMARY.md was actually
        # fetched - otherwise a missing file would silently wipe out a
        # human-entered override ("очікуємо погодження") just because
        # the file wasn't there to read, not because anyone cleared it.
        automation.current_stage_override = summary["current_stage_override"]
    if latest_commit:
        automation.last_commit_message = latest_commit["message"]
        if latest_commit["date"]:
            automation.last_commit_at = datetime.fromisoformat(latest_commit["date"].replace("Z", "+00:00"))

    warnings = []
    if not summary_text:
        warnings.append("dashboard/SUMMARY.md не знайдено — дані неповні (взято тільки з README.md/dashboard/ROI.md).")
    if form_status:
        automation.status = Status(form_status)
    elif summary["status"]:
        try:
            automation.status = Status(summary["status"])
        except ValueError:
            warnings.append(f"Невідомий статус '{summary['status']}' у dashboard/SUMMARY.md — залишив попередній.")

    if selected_dept_ids:
        automation.departments = Department.query.filter(Department.id.in_(selected_dept_ids)).all()
    elif summary_text:
        # Gate on the file having been fetched, not on the parsed list
        # being non-empty - a repo that genuinely lists no departments
        # anymore must clear old ones, not keep whatever synced last time.
        depts = []
        for name in summary["departments"]:
            dept = Department.query.filter_by(name=name).first()
            if not dept:
                dept = Department(name=name, hue=hue_for(name))
                db.session.add(dept)
            depts.append(dept)
        automation.departments = depts

    if summary_text:
        links = []
        skipped = []
        for c in summary["connections"]:
            target = Automation.query.filter_by(slug=c["slug"]).first()
            if target and target.id != automation.id:
                links.append(Connection(connected_automation_id=target.id,
                                         relationship_type=c["relationship_type"]))
            elif c["slug"]:
                skipped.append(c["slug"])
        automation.connections = links
        if skipped:
            warnings.append("Не знайдено (ще?) автоматизації для зв'язку: " + ", ".join(skipped))

    if summary_text:
        # Unlike Departments, don't auto-create a bare Skill row for a
        # name that isn't already in the library - the library is a
        # curated catalog (real description, doc_url, imported from an
        # actual skill repo), not free-text tags, so an unmatched name
        # is reported and skipped instead, same treatment Connections'
        # unmatched slug already gets above.
        skills = []
        skipped_skills = []
        for name in summary["skills"]:
            skill = Skill.query.filter_by(name=name).first()
            if skill:
                skills.append(skill)
            else:
                skipped_skills.append(name)
        automation.skills = skills
        if skipped_skills:
            warnings.append("Не знайдено в бібліотеці скіл(и): " + ", ".join(skipped_skills) +
                             " — спершу додай їх на /skills.")

    if roi_sections:
        fields = github_sync.roi_fields_from_sections(roi_sections)
        if automation.roi is None:
            automation.roi = ROIEntry()
        automation.roi.hypothesis = fields["hypothesis"] or automation.roi.hypothesis
        automation.roi.metric_description = fields["metric_description"] or automation.roi.metric_description
        automation.roi.confidence = fields["confidence"]
        automation.roi.measured_value = fields["measured_value"] or automation.roi.measured_value
        automation.roi.presentation_url = _safe_url(fields["presentation_url"]) or automation.roi.presentation_url
        automation.roi.qualitative_notes = fields["qualitative_notes"] or automation.roi.qualitative_notes
    elif not roi_text:
        warnings.append("dashboard/ROI.md у репозиторії не знайдено.")

    if summary_text:
        # summary_text present means dashboard/SUMMARY.md was actually
        # fetched - an empty "## Pages" section there is a real signal
        # ("this repo has no pages to report"), not a fetch miss, so it
        # must clear stale pages instead of leaving old ones stuck
        # forever (unlike summary_text is None, where SUMMARY.md itself
        # is missing and the existing warning above already covers it).
        function_details = github_sync.parse_functions_md(functions_text)
        if summary["pages"]:
            automation.pages = [
                AutomationPage(name=p["name"], description=p["description"],
                                detail=function_details.get(p["name"]) or None, order_index=i)
                for i, p in enumerate(summary["pages"])
            ]
        elif function_details:
            # Headless automation (no UI screens, so no '## Pages' in
            # SUMMARY.md) - dashboard/functions.md's own sections are
            # still real content, so show them directly instead of
            # losing them entirely for lack of a page to attach to.
            automation.pages = [
                AutomationPage(name=name, description=body, order_index=i)
                for i, (name, body) in enumerate(function_details.items())
            ]
        else:
            automation.pages = []

    if backlog_text is not None:
        # fetch_raw_file only returns None on a confirmed 404 - any other
        # fetch failure raises and aborts the sync before this point, so
        # "file fetched" vs "file missing" is a real, deterministic fact
        # here, not a network blip. Gate on that, not on the parsed list
        # being non-empty, so a cleared-out BACKLOG.md actually clears
        # the dashboard's stale review log instead of leaving it stuck.
        backlog_entries = github_sync.parse_backlog_md(backlog_text, limit=5)
        automation.review_log = [
            ReviewLogEntry(round_label=e["round_label"], found=e["found"],
                            changed=e["changed"], rejected=e["rejected"], order_index=i)
            for i, e in enumerate(backlog_entries)
        ]

    if security_review_text is not None:
        # Same "file fetched" gate as backlog/BACKLOG.md above - absence
        # of the file is the expected, honest "never reviewed" state
        # (Automation.security_review_at defaults to None), not
        # something to warn about; only overwrite once a file is
        # actually there to read, so a transient fetch miss can't
        # silently erase a previously-recorded review.
        sec_fields = github_sync.security_review_fields_from_sections(
            github_sync.parse_security_review_md(security_review_text))
        automation.security_review_at = sec_fields["reviewed_at"]
        automation.security_review_high = sec_fields["high"]
        automation.security_review_medium = sec_fields["medium"]

    if todo_text is not None:
        todo_items = github_sync.parse_todo_md(todo_text)
        automation.todo_items = [
            AutomationTodoItem(text=t["text"], done=t["done"], order_index=i)
            for i, t in enumerate(todo_items)
        ]

    return automation, warnings


_github_org_sync_lock = threading.Lock()


def run_github_org_sync(app, owner):
    """Shared by the `sync-github-org` CLI command (Railway's daily cron) and
    the "Оновити з GitHub" button on /automations (an on-demand run of the
    exact same logic, for when someone doesn't want to wait for 03:00 UTC).
    Returns a human-readable Ukrainian summary line; raises ValueError if
    AUTOMATION_SYNC_OWNER_EMAIL isn't set to a real Automator/Admin (neither
    caller has a logged-in user to fall back to for newly-discovered
    automations), or if a scan is already running in this process (the CLI
    cron and the button both end up calling this, and there's nothing else
    stopping someone from clicking the button twice while the first click's
    scan - up to one GitHub round-trip per repo - is still in flight)."""
    if not _github_org_sync_lock.acquire(blocking=False):
        raise ValueError(f"Синхронізація «{owner}» вже виконується — зачекай, поки попередній запуск завершиться.")
    try:
        owner_email = os.environ.get("AUTOMATION_SYNC_OWNER_EMAIL")
        default_owner = User.query.filter_by(email=owner_email).first() if owner_email else None
        if default_owner is None:
            raise ValueError("AUTOMATION_SYNC_OWNER_EMAIL не задано або не знайдено такого користувача — "
                              "потрібен існуючий Automator/Admin для щойно знайдених автоматизацій.")

        repos = github_sync.list_org_repos(owner)

        imported = updated = pending_count = skipped = failed = 0
        for repo in repos:
            repo_url = f"https://github.com/{owner}/{repo['name']}"
            if repo["archived"]:
                skipped += 1
                continue
            try:
                summary_text = github_sync.fetch_raw_file(
                    owner, repo["name"], "dashboard/SUMMARY.md", repo["default_branch"])
                if summary_text is None:
                    # No dashboard/SUMMARY.md doesn't prove this is a real
                    # automation - some repos in the org genuinely aren't one
                    # (an SDK package, a skills workspace). But guessing that
                    # from repo content is unreliable in the other direction too
                    # (a real product with no PIPELINE.md looks the same as an
                    # SDK from here) - a first pass here only tracked repos with
                    # PIPELINE.md and missed a real one without it. Track every
                    # non-archived repo instead, and let an automator dismiss
                    # whatever turns out not to be an automation - see the
                    # dismiss route below, which this loop never overrides.
                    pipeline_text = github_sync.fetch_raw_file(
                        owner, repo["name"], "PIPELINE.md", repo["default_branch"])
                    missing = (
                        "dashboard/SUMMARY.md (стейдж-0 пройдено) — bootstrap почато (є PIPELINE.md), "
                        "але не завершено." if pipeline_text is not None
                        else "dashboard/SUMMARY.md (стейдж-0 ще не проходив) — bootstrap ще не починався "
                        "(немає PIPELINE.md)."
                    )
                    existing_pending = PendingAutomation.query.filter_by(repo_url=repo_url).first()
                    if existing_pending is None:
                        existing_pending = PendingAutomation(
                            slug=repo["name"].lower(), name=repo["name"], repo_url=repo_url,
                            missing=missing)
                        db.session.add(existing_pending)
                    else:
                        existing_pending.missing = missing
                    existing_pending.last_seen_at = _now()
                    db.session.commit()
                    pending_count += 1
                    continue

                # This repo now has dashboard/SUMMARY.md - if an earlier run
                # tracked it as pending, it just graduated to a real automation,
                # so that placeholder row no longer belongs on the "incomplete"
                # list.
                PendingAutomation.query.filter_by(repo_url=repo_url).delete()

                slug = repo["name"].lower()
                # Match by repo_url first - an automation registered by hand or
                # via the single-repo import form almost never has slug ==
                # repo-name-lowercased (a human picks their own slug), so
                # matching on slug alone would silently create a duplicate
                # automation for a repo that's already registered under a
                # different slug. Only fall back to slug for a repo this same
                # command already created on an earlier run (which does use
                # this exact convention).
                existing = (Automation.query.filter_by(repo_url=repo_url).first()
                            or Automation.query.filter_by(slug=slug).first())
                owner_id = existing.owner_id if existing else default_owner.id
                sync_automation_from_github(existing, repo_url, owner_id, "", [], slug=slug,
                                             prefetched_summary_text=summary_text)
                db.session.commit()
                if existing:
                    updated += 1
                else:
                    imported += 1
            except Exception:
                # Anything above - a fetch timeout, a transient GitHub error,
                # a DB constraint failure - is this one repo's problem, not a
                # reason to abort the whole org scan (and definitely not the
                # same thing as list_org_repos itself failing, which is what
                # both callers report if this propagates instead of being
                # caught here). Repos already committed earlier in this loop
                # stay committed either way.
                db.session.rollback()
                app.logger.exception("sync-github-org: failed processing %s/%s", owner, repo["name"])
                failed += 1
                continue
        summary = (f"{owner}: {imported} нових, {updated} оновлено, {pending_count} неповних "
                   f"(немає dashboard/SUMMARY.md), {skipped} архівованих пропущено.")
        if failed:
            summary += f" {failed} репозиторіїв пропущено через помилку (див. логи)."
        return summary
    finally:
        _github_org_sync_lock.release()


def register_routes(app):
    @app.route("/")
    def index():
        return redirect(url_for("automations_list"))

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(email=email).first()
            if user and user.check_password(password):
                if not user.is_confirmed:
                    flash("Спочатку підтверди код, який тобі назве адміністратор.", "info")
                    return redirect(url_for("confirm", email=email))
                if not user.is_approved:
                    flash("Код підтверджено, але адміністратор ще не надав доступ у Telegram-боті.", "info")
                    return render_template("login.html")
                login_user(user)
                return redirect(url_for("automations_list"))
            flash("Невірний email або пароль.", "error")
        return render_template("login.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            name = request.form.get("name", "").strip()
            password = request.form.get("password", "")
            password_confirm = request.form.get("password_confirm", "")
            if not email or not name or not password:
                flash("Заповни всі поля.", "error")
                return render_template("register.html")
            if len(password) < 8:
                flash("Пароль має бути щонайменше 8 символів.", "error")
                return render_template("register.html")
            if password != password_confirm:
                flash("Паролі не збігаються.", "error")
                return render_template("register.html")

            existing = User.query.filter_by(email=email).first()
            if existing and (existing.is_confirmed or existing.is_approved):
                flash("Акаунт з такою поштою вже існує — увійди звичайним способом.", "error")
                return render_template("register.html")

            user = existing or User(email=email, role=Role.VIEWER)
            user.name = name
            user.set_password(password)
            user.pending_code = f"{secrets.randbelow(1_000_000):06d}"
            user.pending_code_expires_at = _now().replace(tzinfo=None) + timedelta(minutes=30)
            user.pending_code_attempts = 0
            if existing is None:
                db.session.add(user)
            db.session.commit()

            sent = telegram.send_message(
                os.environ.get("ADMIN_TELEGRAM_CHAT_ID"),
                f"Нова реєстрація на дашборді: {name} <{email}>.\n"
                f"Код підтвердження: {user.pending_code} (дійсний 30 хв).\n"
                f"Передай код людині, а після підтвердження встанови права: "
                f"/grant {email} viewer (або automator/admin).",
            )
            if not sent:
                flash("Реєстрацію збережено, але не вдалося сповістити адміністратора в Telegram — "
                      "звернись до нього напряму.", "warning")
            else:
                flash("Реєстрацію подано. Введи код підтвердження, який тобі назве адміністратор.", "info")
            return redirect(url_for("confirm", email=email))
        return render_template("register.html")

    MAX_CONFIRM_ATTEMPTS = 5

    @app.route("/confirm", methods=["GET", "POST"])
    def confirm():
        email = request.values.get("email", "").strip().lower()
        if request.method == "POST":
            code = request.form.get("code", "").strip()
            user = User.query.filter_by(email=email).first()
            if not user or not user.pending_code:
                flash("Акаунт не знайдено, або код уже використано.", "error")
            elif user.pending_code_expires_at and user.pending_code_expires_at < _now().replace(tzinfo=None):
                flash("Код застарів — попроси адміністратора зареєструвати тебе ще раз.", "error")
            elif code != user.pending_code:
                # A 6-digit code has only a million possible values - without
                # this, nothing stops guessing all of them inside the
                # 30-minute window. Past the threshold, invalidate the code
                # outright rather than just keep counting: the only way
                # forward is a fresh /register, which issues a new code and
                # resets this counter.
                user.pending_code_attempts += 1
                if user.pending_code_attempts >= MAX_CONFIRM_ATTEMPTS:
                    user.pending_code = None
                    user.pending_code_expires_at = None
                    db.session.commit()
                    flash("Забагато невірних спроб — код анульовано. Зареєструйся ще раз, щоб отримати новий.", "error")
                else:
                    db.session.commit()
                    flash("Невірний код.", "error")
            else:
                user.is_confirmed = True
                user.pending_code = None
                user.pending_code_expires_at = None
                user.pending_code_attempts = 0
                db.session.commit()
                flash("Акаунт підтверджено. Очікуй, поки адміністратор надасть доступ у Telegram-боті.", "success")
                return redirect(url_for("login"))
        return render_template("confirm.html", email=email)

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("login"))

    @app.route("/automations")
    @login_required
    def automations_list():
        query = Automation.query
        status_filter = request.args.get("status")
        dept_filter = request.args.get("department", type=int)
        search = request.args.get("q", "").strip()

        if status_filter:
            query = query.filter(Automation.status == Status(status_filter))
        if dept_filter:
            query = query.filter(Automation.departments.any(Department.id == dept_filter))
        if search:
            query = query.filter(Automation.name.ilike(f"%{search}%"))

        automations = query.order_by(Automation.updated_at.desc()).all()
        counts = {s: Automation.query.filter_by(status=s).count() for s in Status}
        departments = Department.query.order_by(Department.name).all()
        pending = (PendingAutomation.query.filter_by(dismissed=False)
                   .order_by(PendingAutomation.discovered_at.desc()).all())
        # Portfolio-level KPI strip (design audit, Critical #4): `counts` was
        # already computed above but only ever spent on the status <select>'s
        # parenthetical text - nothing on this page gave an at-a-glance signal
        # of portfolio health before. total_count is unfiltered (all
        # automations regardless of the current search/status/department
        # filter), matching how `counts` itself already behaves.
        total_count = Automation.query.count()
        needs_review_count = Automation.query.filter(Automation.security_review_at.is_(None)).count()
        return render_template(
            "automations_list.html",
            automations=automations,
            statuses=Status,
            counts=counts,
            total_count=total_count,
            needs_review_count=needs_review_count,
            departments=departments,
            active_status=status_filter,
            active_department=dept_filter,
            search=search,
            pending=pending,
        )

    @app.route("/automations/sync-github-org", methods=["POST"])
    @login_required
    @automator_required
    def automations_sync_github_org():
        """The "Оновити з GitHub" button on /automations - runs the exact
        same org-wide scan as the `sync-github-org` cron (Railway only fires
        it once a day, 03:00 UTC), on demand, for whoever doesn't want to
        wait for a newly-created repo or a just-added dashboard/SUMMARY.md
        to show up tomorrow."""
        owner = os.environ.get("GITHUB_SYNC_ORG")
        if not owner:
            flash("GITHUB_SYNC_ORG не задано в .env — не знаю, яку GitHub-організацію сканувати.", "error")
            return redirect(url_for("automations_list"))
        try:
            flash(run_github_org_sync(app, owner), "success")
        except ValueError as e:
            flash(str(e), "error")
        except Exception:
            app.logger.exception("automations_sync_github_org: failed for %s", owner)
            flash(f"Не вдалося отримати список репозиторіїв «{owner}».", "error")
        return redirect(url_for("automations_list"))

    def validate_automation_form(form, automation=None):
        """Field -> error message, for the things that would otherwise either
        silently corrupt data (a bad numeric string past a Numeric column) or
        crash the request outright at commit time (a duplicate slug hitting
        the DB's own UNIQUE constraint as an unhandled IntegrityError). Not a
        full validation framework - just the concrete failure modes this form
        could actually hit, checked before apply_manual_form touches the DB."""
        errors = {}
        if not form.get("name", "").strip():
            errors["name"] = "Вкажи назву."

        if automation is None:
            slug = form.get("slug", "").strip()
            if not slug:
                errors["slug"] = "Вкажи slug."
            elif not re.fullmatch(r"[a-z0-9-]+", slug):
                errors["slug"] = "Тільки латинські малі літери, цифри й дефіс."
            elif Automation.query.filter_by(slug=slug).first():
                errors["slug"] = f"Автоматизація зі slug «{slug}» вже існує."

        for field, label in [("monthly_token_budget_usd", "Місячний бюджет"),
                              ("token_spike_multiplier", "Множник сплеску")]:
            raw = form.get(field, "").strip()
            if raw:
                try:
                    float(raw)
                except ValueError:
                    errors[field] = f"{label}: введи число."

        raw_usage_id = form.get("ai_usage_project_id", "").strip()
        if raw_usage_id and not raw_usage_id.isdigit():
            errors["ai_usage_project_id"] = "ID проєкту: тільки цифри."

        return errors

    def apply_manual_form(automation, form):
        automation.name = form["name"].strip()
        automation.one_liner = form.get("one_liner", "").strip()
        automation.status = Status(form["status"])
        # Only an Admin may hand an automation to someone else - an
        # Automator's own automations always stay owned by them, regardless
        # of what the (hidden, for them) owner field in the form said.
        if current_user.is_admin:
            automation.owner_id = int(form.get("owner_id") or automation.owner_id)
        else:
            automation.owner_id = current_user.id
        automation.repo_url = _safe_url(form.get("repo_url", ""))
        automation.clickup_url = _safe_url(form.get("clickup_url", ""))
        raw_usage_project_id = form.get("ai_usage_project_id", "").strip()
        automation.ai_usage_project_id = int(raw_usage_project_id) if raw_usage_project_id.isdigit() else None
        automation.monthly_token_budget_usd = form.get("monthly_token_budget_usd", "").strip() or None
        automation.token_spike_multiplier = form.get("token_spike_multiplier", "").strip() or None
        if automation.ai_usage_project_id is not None:
            # Nothing stops two cards pointing at the same ai-usage-collector
            # project (a shared OpenAI key is a real setup) - not blocked,
            # but worth a heads-up since both cards will then show/alert on
            # the exact same spend as if it were each one's own.
            dupe = Automation.query.filter(
                Automation.ai_usage_project_id == automation.ai_usage_project_id,
                Automation.id != automation.id,
            ).first()
            if dupe:
                flash(f"Увага: project ID {automation.ai_usage_project_id} в ai-usage-collector вже "
                      f"прив'язаний до «{dupe.name}» — витрати й алерти по бюджету рахуватимуться "
                      f"однаково для обох карток.", "warning")
        automation.departments = Department.query.filter(
            Department.id.in_(form.getlist("departments"))).all()
        automation.skills = Skill.query.filter(Skill.id.in_(form.getlist("skills"))).all()
        if automation.roi is None:
            automation.roi = ROIEntry()
        automation.roi.hypothesis = form.get("hypothesis", "").strip()
        automation.roi.metric_description = form.get("metric_description", "").strip()
        automation.roi.confidence = form.get("confidence", "estimated")
        automation.roi.presentation_url = _safe_url(form.get("presentation_url", ""))

    @app.route("/automations/new", methods=["GET", "POST"])
    @login_required
    @automator_required
    def automation_new():
        users = User.query.order_by(User.name).all()
        departments = Department.query.order_by(Department.name).all()
        skills = Skill.query.order_by(Skill.name).all()
        if request.method == "POST":
            errors = validate_automation_form(request.form)
            if not errors:
                automation = Automation(slug=request.form["slug"].strip(), owner_id=current_user.id)
                apply_manual_form(automation, request.form)
                db.session.add(automation)
                db.session.commit()
                # Straight to the Stage 1 interview next, not the detail page -
                # this is the point where answering it is cheapest (the automator
                # is already here filling in the basics), and stage1_form.html
                # itself links onward to automation_detail so it's a detour, not
                # a dead end.
                return redirect(url_for("automation_stage1", slug=automation.slug))
            flash("Форма містить помилки - перевір позначені поля.", "error")
            return render_template("automation_form.html", departments=departments, skills=skills,
                                    users=users, statuses=Status, automation=None, form_data=request.form,
                                    errors=errors, default_spike_multiplier=ai_usage.SPIKE_MULTIPLIER_DEFAULT,
                                    ai_usage_projects=ai_usage.list_projects())
        return render_template("automation_form.html", departments=departments, skills=skills,
                                users=users, statuses=Status, automation=None,
                                default_spike_multiplier=ai_usage.SPIKE_MULTIPLIER_DEFAULT,
                                ai_usage_projects=ai_usage.list_projects())

    @app.route("/automations/<slug>/edit", methods=["GET", "POST"])
    @login_required
    def automation_edit(slug):
        automation = Automation.query.filter_by(slug=slug).first_or_404()
        if not current_user.can_manage(automation):
            abort(403)
        users = User.query.order_by(User.name).all()
        departments = Department.query.order_by(Department.name).all()
        skills = Skill.query.order_by(Skill.name).all()
        if request.method == "POST":
            errors = validate_automation_form(request.form, automation=automation)
            if not errors:
                apply_manual_form(automation, request.form)
                db.session.commit()
                return redirect(url_for("automation_detail", slug=automation.slug))
            flash("Форма містить помилки - перевір позначені поля.", "error")
            return render_template("automation_form.html", departments=departments, skills=skills,
                                    users=users, statuses=Status, automation=automation, form_data=request.form,
                                    errors=errors, default_spike_multiplier=ai_usage.SPIKE_MULTIPLIER_DEFAULT,
                                    ai_usage_projects=ai_usage.list_projects())
        return render_template("automation_form.html", departments=departments, skills=skills,
                                users=users, statuses=Status, automation=automation,
                                default_spike_multiplier=ai_usage.SPIKE_MULTIPLIER_DEFAULT,
                                ai_usage_projects=ai_usage.list_projects())

    @app.route("/automations/<slug>/stage1", methods=["GET", "POST"])
    @login_required
    def automation_stage1(slug):
        """The Stage 1 interview (src/stage1_questions.py), filled in the
        dashboard so a later `stage-1-supplax` bootstrap run can pull it via
        the read-only API endpoint below and skip whatever's already
        answered here, instead of asking live in a Claude Code session."""
        automation = Automation.query.filter_by(slug=slug).first_or_404()
        if not current_user.can_manage(automation):
            abort(403)
        if request.method == "POST":
            automation.stage1_answers = stage1_questions.collect_answers(request.form)
            db.session.commit()
            n = stage1_questions.answered_phase_count(automation.stage1_answers)
            flash(f"Відповіді Stage 1 збережено ({n}/9 фаз).", "success")
            return redirect(url_for("automation_detail", slug=automation.slug))
        return render_template("stage1_form.html", automation=automation,
                                phases=stage1_questions.PHASES,
                                answers=automation.stage1_answers or {})

    @app.route("/automations/import-github", methods=["GET", "POST"])
    @login_required
    @automator_required
    def automation_import_github():
        users = User.query.order_by(User.name).all()
        departments = Department.query.order_by(Department.name).all()
        if request.method == "POST":
            repo_url = request.form["repo_url"].strip()
            slug = request.form.get("slug", "").strip() or None
            existing = Automation.query.filter_by(slug=slug).first() if slug else None
            if existing and not current_user.can_manage(existing):
                flash("Ця автоматизація вже зареєстрована іншим автоматизатором.", "error")
                return redirect(url_for("automation_import_github"))
            owner_id = int(request.form["owner_id"]) if current_user.is_admin else current_user.id
            try:
                automation, warnings = sync_automation_from_github(
                    existing, repo_url, owner_id,
                    request.form.get("status") or "", request.form.getlist("departments"), slug=slug)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("automation_import_github"))
            except Exception:
                db.session.rollback()
                app.logger.exception("GitHub import failed for %s", repo_url)
                flash("Не вдалося звернутися до GitHub — перевір посилання і чи репозиторій публічний "
                      "(або що GITHUB_TOKEN в .env дійсний, якщо приватний).", "error")
                return redirect(url_for("automation_import_github"))

            db.session.commit()
            flash(f"Синхронізовано з {repo_url}." + (" " + " ".join(warnings) if warnings else ""), "success")
            return redirect(url_for("automation_detail", slug=automation.slug))

        prefill_slug = request.args.get("slug", "")
        prefill_owner_id = request.args.get("owner_id", type=int)
        return render_template("automation_import.html", users=users, departments=departments, statuses=Status,
                                prefill_slug=prefill_slug, prefill_owner_id=prefill_owner_id)

    @app.route("/automations/<slug>/resync", methods=["POST"])
    @login_required
    def automation_resync(slug):
        """The per-automation 'Оновити з GitHub' button - re-fetches from the
        repo_url already on file, no form to refill."""
        automation = Automation.query.filter_by(slug=slug).first_or_404()
        if not current_user.can_manage(automation):
            abort(403)
        repo_url = automation.repo_url
        if not repo_url:
            flash("У цієї автоматизації не вказано посилання на репозиторій.", "error")
            return redirect(url_for("automation_detail", slug=slug))
        try:
            automation, warnings = sync_automation_from_github(
                automation, repo_url, automation.owner_id, "", [])
        except Exception:
            # Roll back first - a failed flush (e.g. a DB constraint error)
            # leaves the session unusable, and touching any ORM attribute
            # (even automation.repo_url, already read above into a plain
            # local var precisely to avoid this) before rolling back raises
            # a second, unrelated PendingRollbackError that masks the real one.
            db.session.rollback()
            app.logger.exception("GitHub resync failed for %s", repo_url)
            flash("Не вдалося звернутися до GitHub — перевір, чи репозиторій усе ще доступний.", "error")
            return redirect(url_for("automation_detail", slug=slug))

        db.session.commit()
        flash("Оновлено з GitHub." + (" " + " ".join(warnings) if warnings else ""), "success")
        return redirect(url_for("automation_detail", slug=automation.slug))

    @app.route("/automations/pending/<int:pending_id>/dismiss", methods=["POST"])
    @login_required
    @automator_required
    def pending_automation_dismiss(pending_id):
        """Marks a PendingAutomation as a false positive (a repo with
        PIPELINE.md that was never actually meant to become a tracked
        automation) - sync-github-org's upsert never clears this flag on its
        own, so a dismissed repo stays hidden on every later run."""
        pending = PendingAutomation.query.get_or_404(pending_id)
        pending.dismissed = True
        db.session.commit()
        flash(f"«{pending.name}» приховано зі списку неповних автоматизацій.", "success")
        return redirect(url_for("automations_list"))

    @app.route("/automations/<slug>")
    @login_required
    def automation_detail(slug):
        automation = Automation.query.filter_by(slug=slug).first_or_404()
        usage_summary = ai_usage.get_usage_summary(automation) if automation.ai_usage_project_id else None
        return render_template("automation_detail.html", automation=automation, usage_summary=usage_summary)

    @app.route("/departments")
    @login_required
    @admin_required
    def departments_list():
        departments = Department.query.order_by(Department.name).all()
        return render_template("departments.html", departments=departments)

    @app.route("/departments/<int:dept_id>/rename", methods=["POST"])
    @login_required
    @admin_required
    def department_rename(dept_id):
        dept = Department.query.get_or_404(dept_id)
        new_name = request.form.get("name", "").strip()
        if not new_name:
            flash("Назва відділу не може бути порожньою.", "error")
        elif Department.query.filter(Department.name == new_name, Department.id != dept_id).first():
            flash(f"Відділ з назвою «{new_name}» вже існує — злий їх через об'єднання нижче, а не перейменування.", "error")
        else:
            dept.name = new_name
            db.session.commit()
        return redirect(url_for("departments_list"))

    @app.route("/departments/merge", methods=["POST"])
    @login_required
    @admin_required
    def department_merge():
        from_dept = Department.query.get_or_404(int(request.form["from_id"]))
        into_dept = Department.query.get_or_404(int(request.form["into_id"]))
        if from_dept.id == into_dept.id:
            flash("Неможливо об'єднати відділ сам із собою.", "error")
            return redirect(url_for("departments_list"))
        for automation in list(from_dept.automations):
            if into_dept not in automation.departments:
                automation.departments.append(into_dept)
            automation.departments.remove(from_dept)
        db.session.delete(from_dept)
        db.session.commit()
        flash(f"«{from_dept.name}» об'єднано з «{into_dept.name}».", "success")
        return redirect(url_for("departments_list"))

    @app.route("/departments/<int:dept_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    def department_delete(dept_id):
        dept = Department.query.get_or_404(dept_id)
        if dept.automations:
            flash(f"«{dept.name}» використовується у {len(dept.automations)} автоматизаціях — "
                  f"спочатку об'єднай з іншим відділом, потім видаляй.", "error")
        else:
            db.session.delete(dept)
            db.session.commit()
        return redirect(url_for("departments_list"))

    @app.route("/automators/<int:user_id>")
    @login_required
    def automator_profile(user_id):
        automator = User.query.get_or_404(user_id)
        return render_template("automator_profile.html", automator=automator)

    @app.route("/automators/<int:user_id>/regenerate-api-key", methods=["POST"])
    @login_required
    def regenerate_api_key(user_id):
        """Self-service only - there's no admin override here on purpose.
        A self-registered Automator/Admin never sees their api_key at all
        otherwise (create-user's CLI is the only place that ever prints one);
        this also doubles as the way to invalidate a copy that might have
        leaked, since the old key stops working the moment a new one is
        generated."""
        if current_user.id != user_id:
            abort(403)
        current_user.api_key = secrets.token_hex(32)
        db.session.commit()
        flash(f"Новий API-ключ: {current_user.api_key} — збережи його зараз, більше він ніде не покажеться.", "success")
        return redirect(url_for("automator_profile", user_id=user_id))

    @app.route("/skills")
    @login_required
    def skills_library():
        skills = Skill.query.order_by(Skill.name).all()
        return render_template("skills_library.html", skills=skills)

    @app.route("/skills/<int:skill_id>/download")
    @login_required
    def skill_download(skill_id):
        """Packages the skill's folder (SKILL.md plus any references/
        scripts/templates) straight from GitHub into a zip, for someone who
        wants the skill itself rather than just a link to read it - a skill
        with no repo_url (added via the API sync payload or seed-demo, never
        through /skills/import-github) has nothing to fetch, so there's
        nothing to download."""
        skill = Skill.query.get_or_404(skill_id)
        if not skill.repo_url:
            abort(404)
        parsed = github_sync.parse_repo_or_folder_url(skill.repo_url)
        if not parsed:
            abort(404)
        owner_gh, repo, branch, path = parsed
        if branch is None:
            branch = github_sync.default_branch(owner_gh, repo)
        try:
            files = github_sync.fetch_directory_tree(owner_gh, repo, path or "", branch)
        except Exception:
            app.logger.exception("Skill download failed for %s", skill.repo_url)
            flash("Не вдалося завантажити файли скіла з GitHub.", "error")
            return redirect(url_for("skills_library"))
        if not files:
            flash("У цьому скілі не знайдено файлів для завантаження.", "error")
            return redirect(url_for("skills_library"))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_path, content in files.items():
                zf.writestr(rel_path, content)
        buf.seek(0)
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", skill.name).strip("-") or "skill"
        return send_file(buf, mimetype="application/zip", as_attachment=True,
                          download_name=f"{safe_name}.zip")

    def _upsert_skill(name, description, repo_url, doc_url, security_review_text=None):
        skill = Skill.query.filter_by(name=name).first()
        if skill is None:
            skill = Skill(name=name)
            db.session.add(skill)
        skill.description = description or skill.description
        skill.repo_url = repo_url
        skill.doc_url = doc_url
        if security_review_text is not None:
            # Same "file fetched" gate sync_automation_from_github uses for
            # dashboard/SECURITY_REVIEW.md - absence is "never reviewed"
            # (columns stay None), not something to warn about.
            sec_fields = github_sync.security_review_fields_from_sections(
                github_sync.parse_security_review_md(security_review_text))
            skill.security_review_at = sec_fields["reviewed_at"]
            skill.security_review_high = sec_fields["high"]
            skill.security_review_medium = sec_fields["medium"]
        return skill

    def sync_skill_from_github(repo_url):
        """One repo = one skill, its SKILL.md at repo root - the shape every
        skill under ~/.claude/skills already has. Looks up an existing Skill
        by the parsed name first, so re-importing the same (or a
        renamed-URL-but-same-name) repo updates that row instead of creating
        a duplicate entry. Raises on a GitHub fetch failure or a missing/
        malformed SKILL.md - the caller turns that into a flash message."""
        parsed = github_sync.parse_repo_or_folder_url(repo_url)
        if not parsed:
            raise ValueError("Не схоже на посилання на GitHub-репозиторій "
                              "(очікую https://github.com/власник/репо).")
        owner_gh, repo, branch, path = parsed
        if branch is None:
            branch = github_sync.default_branch(owner_gh, repo)
        skill_path = f"{path}/SKILL.md" if path else "SKILL.md"

        skill_text = github_sync.fetch_raw_file(owner_gh, repo, skill_path, branch)
        if skill_text is None:
            raise ValueError(f"SKILL.md не знайдено за шляхом «{skill_path}».")
        fields = github_sync.parse_skill_md(skill_text)
        if not fields.get("name"):
            raise ValueError("SKILL.md знайдено, але в ньому немає поля 'name' у frontmatter.")

        security_review_path = f"{path}/dashboard/SECURITY_REVIEW.md" if path else "dashboard/SECURITY_REVIEW.md"
        security_review_text = github_sync.fetch_raw_file(owner_gh, repo, security_review_path, branch)
        return _upsert_skill(fields["name"], fields.get("description"), repo_url,
                              f"https://github.com/{owner_gh}/{repo}/blob/{branch}/{skill_path}",
                              security_review_text=security_review_text)

    def sync_skills_from_github_folder(repo_url):
        """Bulk import: repo_url points at a folder
        (.../tree/<branch>/<path>) holding several skills as subdirectories,
        each with its own SKILL.md - Supplax's shared-skills-repo convention
        (e.g. giga-brdg/Skills_Supplax's .claude/skills/), alongside the
        one-repo-per-skill convention sync_skill_from_github covers.
        Best-effort per subdirectory: one missing/malformed SKILL.md is
        reported and skipped, not a fatal error for the whole batch - a
        shared repo commonly mixes real skills with a stray non-skill
        folder. Raises only when the folder itself can't be read at all."""
        parsed = github_sync.parse_repo_or_folder_url(repo_url)
        if not parsed:
            raise ValueError("Не схоже на посилання на GitHub-репозиторій "
                              "(очікую https://github.com/власник/репо).")
        owner_gh, repo, branch, path = parsed
        if branch is None:
            branch = github_sync.default_branch(owner_gh, repo)
        if not path:
            raise ValueError("Це посилання на весь репозиторій, а не на папку зі скілами — "
                              "встав посилання виду .../tree/<гілка>/шлях/до/папки.")

        entries = github_sync.list_directory(owner_gh, repo, path, branch)
        if entries is None:
            raise ValueError(f"Папку «{path}» не знайдено в цьому репозиторії.")
        subdirs = [e["name"] for e in entries if e["type"] == "dir"]
        if not subdirs:
            raise ValueError(f"У папці «{path}» немає підпапок зі скілами.")

        imported, skipped = [], []
        for name in subdirs:
            skill_path = f"{path}/{name}/SKILL.md"
            skill_text = github_sync.fetch_raw_file(owner_gh, repo, skill_path, branch)
            if skill_text is None:
                skipped.append(f"{name} (немає SKILL.md)")
                continue
            fields = github_sync.parse_skill_md(skill_text)
            if not fields.get("name"):
                skipped.append(f"{name} (SKILL.md без поля 'name')")
                continue
            security_review_text = github_sync.fetch_raw_file(
                owner_gh, repo, f"{path}/{name}/dashboard/SECURITY_REVIEW.md", branch)
            _upsert_skill(fields["name"], fields.get("description"),
                          f"https://github.com/{owner_gh}/{repo}/tree/{branch}/{path}/{name}",
                          f"https://github.com/{owner_gh}/{repo}/blob/{branch}/{skill_path}",
                          security_review_text=security_review_text)
            imported.append(fields["name"])
        return imported, skipped

    @app.route("/skills/import-github", methods=["GET", "POST"])
    @login_required
    @automator_required
    def skill_import_github():
        if request.method == "POST":
            repo_url = request.form["repo_url"].strip()
            is_folder = "/tree/" in repo_url
            try:
                if is_folder:
                    imported, skipped = sync_skills_from_github_folder(repo_url)
                else:
                    skill = sync_skill_from_github(repo_url)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("skill_import_github"))
            except Exception:
                db.session.rollback()
                app.logger.exception("GitHub import failed for skill(s) %s", repo_url)
                flash("Не вдалося звернутися до GitHub — перевір посилання і чи репозиторій публічний "
                      "(або що GITHUB_TOKEN в .env дійсний, якщо приватний).", "error")
                return redirect(url_for("skill_import_github"))

            db.session.commit()
            if is_folder:
                if imported:
                    msg = f"Імпортовано {len(imported)} скіл(ів): {', '.join(imported)}."
                else:
                    msg = "Жодного скіла не імпортовано."
                if skipped:
                    msg += " Пропущено: " + ", ".join(skipped) + "."
                flash(msg, "success" if imported else "warning")
            else:
                flash(f"Синхронізовано скіл «{skill.name}» з {repo_url}.", "success")
            return redirect(url_for("skills_library"))
        return render_template("skill_import.html")

    @app.route("/api/automations/<slug>/sync", methods=["POST"])
    @csrf.exempt  # machine-facing, X-API-Key auth - no Flask session to carry a CSRF token
    def api_sync_automation(slug):
        """Machine-facing endpoint for stage-1-supplax's portfolio-sync step to
        push a full automation record after a build finishes, authenticated by
        the owning automator's personal api_key rather than a browser session.
        Idempotent: safe to call again for the same slug to update it (owner
        must match). Departments and skills named here that don't exist yet
        are auto-created - see references/portfolio-sync.md's payload schema
        for the exact shape expected."""
        api_key = request.headers.get("X-API-Key")
        owner = User.query.filter_by(api_key=api_key).first() if api_key else None
        if not owner:
            return jsonify({"error": "invalid or missing X-API-Key"}), 401
        # is_approved, not just role: /revoke (src/telegram_bot.py) only ever
        # flips is_approved off, and a self-registered account already holds
        # a live api_key from the moment it signs up (User.api_key's column
        # default), long before any admin approval - checking role alone
        # would let a revoked Automator/Admin, or a never-approved
        # self-registration, keep pushing through this endpoint regardless.
        if owner.role not in (Role.ADMIN, Role.AUTOMATOR) or not owner.is_approved:
            return jsonify({"error": "this account isn't allowed to create or manage automations"}), 403

        payload = request.get_json(silent=True) or {}
        if "name" not in payload:
            return jsonify({"error": "'name' is required"}), 400

        automation = Automation.query.filter_by(slug=slug).first()
        if automation is None:
            automation = Automation(slug=slug, owner_id=owner.id, name=payload["name"])
            db.session.add(automation)
        elif automation.owner_id != owner.id:
            return jsonify({"error": "automation exists under a different owner"}), 403

        automation.name = payload.get("name", automation.name)
        automation.one_liner = payload.get("one_liner", automation.one_liner)
        if "repo_url" in payload:
            automation.repo_url = _safe_url(payload["repo_url"])
        if "clickup_url" in payload:
            automation.clickup_url = _safe_url(payload["clickup_url"])
        if "status" in payload:
            try:
                automation.status = Status(payload["status"])
            except ValueError:
                return jsonify({"error": f"unknown status '{payload['status']}'"}), 400

        if "departments" in payload:
            names = [n.strip() for n in payload["departments"] if n.strip()]
            depts = []
            for name in names:
                dept = Department.query.filter_by(name=name).first()
                if not dept:
                    dept = Department(name=name, hue=hue_for(name))
                    db.session.add(dept)
                depts.append(dept)
            automation.departments = depts

        if "skills" in payload:
            names = [n.strip() for n in payload["skills"] if n.strip()]
            skills = []
            for name in names:
                skill = Skill.query.filter_by(name=name).first()
                if not skill:
                    skill = Skill(name=name)
                    db.session.add(skill)
                skills.append(skill)
            automation.skills = skills

        if "roi" in payload:
            roi = payload["roi"] or {}
            if automation.roi is None:
                automation.roi = ROIEntry()
            automation.roi.hypothesis = roi.get("hypothesis", automation.roi.hypothesis)
            automation.roi.metric_description = roi.get("metric_description", automation.roi.metric_description)
            automation.roi.confidence = roi.get("confidence", automation.roi.confidence)
            automation.roi.measured_value = roi.get("measured_value", automation.roi.measured_value)
            if "presentation_url" in roi:
                automation.roi.presentation_url = _safe_url(roi["presentation_url"])

        if "comparison" in payload:
            comp = payload["comparison"] or {}
            if automation.comparison is None:
                automation.comparison = Comparison()
            automation.comparison.old_way_description = comp.get(
                "old_way_description", automation.comparison.old_way_description)
            automation.comparison.limitations = comp.get("limitations", automation.comparison.limitations)
            if "features" in comp:
                automation.comparison.features = [
                    FeatureRow(
                        feature=f.get("feature", ""), old_way=f.get("old_way", ""),
                        new_way=f.get("new_way", ""), why_it_matters=f.get("why_it_matters", ""),
                    )
                    for f in comp["features"]
                ]

        db.session.commit()
        return jsonify({"ok": True, "slug": automation.slug}), 200

    @app.route("/api/automations/<slug>/stage1-answers", methods=["GET"])
    @csrf.exempt  # machine-facing, X-API-Key auth - same reasoning as api_sync_automation above
    def api_stage1_answers(slug):
        """Read-only counterpart to api_sync_automation: lets stage-1-supplax's
        own bootstrap step pull whatever Stage 1 interview answers were
        already filled in on the automation's dashboard page
        (/automations/<slug>/stage1), keyed the same way
        src/stage1_questions.py stores them, so the skill can skip asking
        about anything already present here. Same auth as the push endpoint
        (X-API-Key -> owner lookup -> role + is_approved check) - this is
        automation metadata, not user PII, but it's still gated to an
        approved Automator/Admin, and still only the automation's own owner
        or an admin, matching api_sync_automation's ownership check."""
        api_key = request.headers.get("X-API-Key")
        owner = User.query.filter_by(api_key=api_key).first() if api_key else None
        if not owner:
            return jsonify({"error": "invalid or missing X-API-Key"}), 401
        if owner.role not in (Role.ADMIN, Role.AUTOMATOR) or not owner.is_approved:
            return jsonify({"error": "this account isn't allowed to read automation data"}), 403

        automation = Automation.query.filter_by(slug=slug).first()
        if automation is None:
            return jsonify({"error": "no automation with that slug"}), 404
        if automation.owner_id != owner.id and not owner.is_admin:
            return jsonify({"error": "automation exists under a different owner"}), 403

        if not automation.stage1_answers:
            return jsonify({"error": "no Stage 1 answers filled in yet"}), 404
        return jsonify({"slug": automation.slug, "answers": automation.stage1_answers}), 200


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create all tables."""
        db.create_all()
        click.echo("Database tables created.")

    @app.cli.command("migrate-registration")
    def migrate_registration():
        """One-off schema migration for self-service registration - adds
        user.is_confirmed/is_approved/pending_code/pending_code_expires_at.
        Introspects existing columns first (works against SQLite or
        Postgres, whichever DATABASE_URL points at) so it's safe to run more
        than once. Existing rows get backfilled as already confirmed+approved
        (DEFAULT TRUE on the ADD COLUMN itself) so this can't lock out logins
        that already work - it doesn't matter that the column's DB-level
        default stays TRUE afterwards, since every INSERT going through the
        ORM (e.g. /register) always passes an explicit value from the model's
        own Python-side default=False, never relying on the DB default."""
        existing_cols = {c["name"] for c in db.inspect(db.engine).get_columns("user")}
        statements = []
        if "is_confirmed" not in existing_cols:
            statements.append('ALTER TABLE "user" ADD COLUMN is_confirmed BOOLEAN NOT NULL DEFAULT TRUE')
        if "is_approved" not in existing_cols:
            statements.append('ALTER TABLE "user" ADD COLUMN is_approved BOOLEAN NOT NULL DEFAULT TRUE')
        if "pending_code" not in existing_cols:
            statements.append('ALTER TABLE "user" ADD COLUMN pending_code VARCHAR(10)')
        if "pending_code_expires_at" not in existing_cols:
            statements.append('ALTER TABLE "user" ADD COLUMN pending_code_expires_at TIMESTAMP')
        if not statements:
            click.echo("Already migrated - nothing to do.")
            return
        for stmt in statements:
            db.session.execute(db.text(stmt))
        db.session.commit()
        click.echo(f"Migrated: added {len(statements)} column(s) to user.")

    @app.cli.command("migrate-confirm-attempts")
    def migrate_confirm_attempts():
        """One-off schema migration for /confirm's brute-force guard - adds
        user.pending_code_attempts. Introspects existing columns first, so
        it's safe to run more than once."""
        existing_cols = {c["name"] for c in db.inspect(db.engine).get_columns("user")}
        if "pending_code_attempts" in existing_cols:
            click.echo("Already migrated - nothing to do.")
            return
        db.session.execute(db.text('ALTER TABLE "user" ADD COLUMN pending_code_attempts INTEGER NOT NULL DEFAULT 0'))
        db.session.commit()
        click.echo("Migrated: added pending_code_attempts column to user.")

    @app.cli.command("migrate-skill-repo-url")
    def migrate_skill_repo_url():
        """One-off schema migration for GitHub-imported skills - adds
        skill.repo_url (see /skills/import-github). Introspects existing
        columns first, so it's safe to run more than once."""
        existing_cols = {c["name"] for c in db.inspect(db.engine).get_columns("skill")}
        if "repo_url" in existing_cols:
            click.echo("Already migrated - nothing to do.")
            return
        db.session.execute(db.text("ALTER TABLE skill ADD COLUMN repo_url VARCHAR(500)"))
        db.session.commit()
        click.echo("Migrated: added repo_url column to skill.")

    @app.cli.command("migrate-token-usage")
    def migrate_token_usage():
        """One-off schema migration for ai-usage-collector linkage + budget
        fields on automation (see src/ai_usage.py, check-token-usage).
        Introspects existing columns first, so it's safe to run more than
        once."""
        existing_cols = {c["name"] for c in db.inspect(db.engine).get_columns("automation")}
        statements = []
        if "ai_usage_project_id" not in existing_cols:
            statements.append("ALTER TABLE automation ADD COLUMN ai_usage_project_id INTEGER")
        if "monthly_token_budget_usd" not in existing_cols:
            statements.append("ALTER TABLE automation ADD COLUMN monthly_token_budget_usd NUMERIC(10,2)")
        if "token_spike_multiplier" not in existing_cols:
            statements.append("ALTER TABLE automation ADD COLUMN token_spike_multiplier NUMERIC(4,1)")
        if "last_token_alert_kind" not in existing_cols:
            statements.append("ALTER TABLE automation ADD COLUMN last_token_alert_kind VARCHAR(20)")
        if "last_token_alert_at" not in existing_cols:
            statements.append("ALTER TABLE automation ADD COLUMN last_token_alert_at TIMESTAMP")
        if not statements:
            click.echo("Already migrated - nothing to do.")
            return
        for stmt in statements:
            db.session.execute(db.text(stmt))
        db.session.commit()
        click.echo(f"Migrated: added {len(statements)} column(s) to automation.")

    @app.cli.command("migrate-stage0-answers")
    def migrate_stage0_answers():
        """One-off schema migration for the Stage 0 interview answers column
        (see src/stage0_questions.py, automation_stage0, api_stage0_answers).
        Safe to run more than once."""
        existing_cols = {c["name"] for c in db.inspect(db.engine).get_columns("automation")}
        if "stage0_answers" in existing_cols:
            click.echo("Already migrated - nothing to do.")
            return
        db.session.execute(db.text("ALTER TABLE automation ADD COLUMN stage0_answers JSON"))
        db.session.commit()
        click.echo("Migrated: added stage0_answers column to automation.")

    @app.cli.command("migrate-rename-stage0-to-stage1")
    def migrate_rename_stage0_to_stage1():
        """Renames Automation.stage0_answers -> stage1_answers and the Skill row
        'stage-0-supplax' -> 'stage-1-supplax', now that the skill itself is
        renamed (see src/stage1_questions.py, automation_stage1,
        api_stage1_answers). Safe to run more than once."""
        existing_cols = {c["name"] for c in db.inspect(db.engine).get_columns("automation")}
        if "stage0_answers" in existing_cols and "stage1_answers" not in existing_cols:
            db.session.execute(db.text("ALTER TABLE automation RENAME COLUMN stage0_answers TO stage1_answers"))
            db.session.commit()
            click.echo("Migrated: renamed automation.stage0_answers to stage1_answers.")
        else:
            click.echo("Column already migrated - nothing to do.")

        renamed = Skill.query.filter_by(name="stage-0-supplax").first()
        if renamed:
            renamed.name = "stage-1-supplax"
            db.session.commit()
            click.echo("Migrated: renamed skill 'stage-0-supplax' to 'stage-1-supplax'.")
        else:
            click.echo("No skill row named 'stage-0-supplax' - nothing to do.")

    @app.cli.command("migrate-security-review")
    def migrate_security_review():
        """One-off schema migration for the dashboard/SECURITY_REVIEW.md sync
        columns on both tables SecurityReviewMixin backs (see
        src/github_sync.py's security_review_fields_from_sections,
        sync_automation_from_github, sync_skill_from_github,
        Automation/Skill.security_review_state). Safe to run more than once,
        and safe to re-run after Skill's columns were added later - each
        table's columns are checked independently."""
        statements = []
        for table in ("automation", "skill"):
            existing_cols = {c["name"] for c in db.inspect(db.engine).get_columns(table)}
            if "security_review_at" not in existing_cols:
                statements.append(f"ALTER TABLE {table} ADD COLUMN security_review_at TIMESTAMP")
            if "security_review_high" not in existing_cols:
                statements.append(f"ALTER TABLE {table} ADD COLUMN security_review_high INTEGER")
            if "security_review_medium" not in existing_cols:
                statements.append(f"ALTER TABLE {table} ADD COLUMN security_review_medium INTEGER")
        if not statements:
            click.echo("Already migrated - nothing to do.")
            return
        for stmt in statements:
            db.session.execute(db.text(stmt))
        db.session.commit()
        click.echo(f"Migrated: added {len(statements)} column(s) across automation/skill.")

    @app.cli.command("migrate-pending-automations")
    def migrate_pending_automations():
        """One-off schema migration: creates the pending_automation table
        (see Models.PendingAutomation, sync-github-org below). Safe to run
        more than once - uses checkfirst so an existing table is a no-op
        rather than an error."""
        existing_tables = set(db.inspect(db.engine).get_table_names())
        if "pending_automation" in existing_tables:
            click.echo("Already migrated - nothing to do.")
            return
        PendingAutomation.__table__.create(db.engine, checkfirst=True)
        click.echo("Migrated: created pending_automation table.")

    @app.cli.command("sync-github-org")
    @click.argument("owner", required=False)
    def sync_github_org(owner):
        """One-shot bulk sync of every repo under a GitHub org or user
        account (`owner`, e.g. "giga-brdg") that has a dashboard/SUMMARY.md -
        meant to be invoked by a Railway Cron Schedule, same convention as
        check-token-usage below. `owner` defaults to the GITHUB_SYNC_ORG env
        var (the same one the "Оновити з GitHub" web button reads) so the
        org name has one source of truth instead of two config surfaces
        that can drift apart - pass it explicitly only to sync a different
        org one-off. NOT run continuously, and NOT the same bar
        as the single-repo /automations/import-github form: a repo with no
        dashboard/SUMMARY.md never becomes a real Automation here (no
        README-only stub), because nothing distinguishes a real automation
        from any other repo in the org (an SDK, this dashboard's own repo)
        except that file. It also doesn't get silently dropped, though -
        whether or not it has PIPELINE.md (stage-1-supplax's own bootstrap
        marker - present means it *was* set up as an automation and just
        never finished reaching the dashboard; absent just means nobody's
        confirmed it either way), every non-archived repo without
        dashboard/SUMMARY.md gets tracked as a PendingAutomation so it shows
        up on /automations as "known but incomplete," with `missing` noting
        whether stage-0 ran or not. An automator dismisses whatever turns
        out not to be a real automation (an SDK, a skills workspace) via
        that page's own button - this command never un-dismisses one on a
        later run. Only an archived repo is skipped outright, no tracking.
        A repo already registered as a real Automation keeps its current
        owner on every re-run; a brand-new one is assigned to
        AUTOMATION_SYNC_OWNER_EMAIL, which must name an existing
        Automator/Admin - there's no logged-in user to fall back to."""
        owner = owner or os.environ.get("GITHUB_SYNC_ORG")
        if not owner:
            click.echo("Вкажи owner аргументом, або задай GITHUB_SYNC_ORG у .env.")
            return
        try:
            summary = run_github_org_sync(app, owner)
        except ValueError as e:
            click.echo(str(e))
            return
        except Exception:
            app.logger.exception("sync-github-org: could not list repos for %s", owner)
            click.echo(f"Не вдалося отримати список репозиторіїв «{owner}».")
            return
        click.echo(summary)

    @app.cli.command("check-token-usage")
    def check_token_usage():
        """One-shot budget/spike check against ai-usage-collector's data,
        meant to be invoked by a Railway Cron Schedule on its own service
        (see docs/token_usage_alerts_plan.md) - NOT run continuously.
        Idempotent per threshold-cross via Automation.last_token_alert_kind/
        _at. Must exit promptly: Railway skips the next scheduled cron run
        if this one is still marked Active."""
        admin_chat_id = os.environ.get("ADMIN_TELEGRAM_CHAT_ID")
        checked = alerted = 0
        automations = Automation.query.filter(Automation.ai_usage_project_id.isnot(None)).all()
        for automation in automations:
            status = ai_usage.get_budget_and_spike_status(automation)
            if status is None:
                continue
            checked += 1
            multiplier = float(automation.token_spike_multiplier or ai_usage.SPIKE_MULTIPLIER_DEFAULT)
            kind, message = _evaluate_token_alert(automation, status, multiplier)
            if kind and kind != automation.last_token_alert_kind:
                if telegram.send_message(admin_chat_id, message):
                    automation.last_token_alert_kind = kind
                    automation.last_token_alert_at = _now()
                    alerted += 1
            elif kind is None and automation.last_token_alert_kind:
                # Back under every threshold - clear so a future re-cross alerts again.
                automation.last_token_alert_kind = None
        db.session.commit()
        click.echo(f"Checked {checked} automation(s), sent {alerted} alert(s).")

    def _evaluate_token_alert(automation, status, multiplier):
        """Priority: budget >=100% > budget >=80% > spike > no budget set -
        a critical budget breach matters more than a same-day spike, and a
        same-day spike is a more urgent signal than the fact that nobody
        set a budget at all. That last one only fires once (dedup'd like
        every other kind via last_token_alert_kind) and only when there's
        real spend to warn about - a linked-but-silent project with zero
        spend has nothing to nudge anyone about yet. Returns (kind,
        message) or (None, None)."""
        if status["budget_usd"] and status["budget_pct"] is not None:
            if status["budget_pct"] >= ai_usage.BUDGET_CRITICAL_THRESHOLD:
                return "budget_100", (
                    f"🔴 {automation.name}: витрачено ${status['month_spend_usd']:.2f} "
                    f"з бюджету ${status['budget_usd']:.2f} цього місяця (100%+)."
                )
            if status["budget_pct"] >= ai_usage.BUDGET_WARN_THRESHOLD:
                return "budget_80", (
                    f"🟠 {automation.name}: витрачено ${status['month_spend_usd']:.2f} "
                    f"з бюджету ${status['budget_usd']:.2f} цього місяця (80%+)."
                )
        if status["trailing_7d_avg_usd"] and status["today_spend_usd"] > multiplier * status["trailing_7d_avg_usd"]:
            return "spike", (
                f"⚡ {automation.name}: сьогоднішні витрати ${status['today_spend_usd']:.2f} "
                f"у {multiplier}x+ вищі за середнє за 7 днів (${status['trailing_7d_avg_usd']:.2f})."
            )
        if not status["budget_usd"] and status["has_any_data"] and status["month_spend_usd"] > 0:
            return "no_budget", (
                f"ℹ️ {automation.name}: витрачено ${status['month_spend_usd']:.2f} цього місяця, "
                f"але місячний бюджет не задано — алерти по бюджету для цієї автоматизації не спрацюють, "
                f"доки його не вказати на формі."
            )
        return None, None

    _SKILL_DESCRIPTIONS_UK = {
        "automation-portfolio-sync": "Перевіряє, чи репозиторій автоматизації готовий до синку з цим "
            "дашбордом, і сам виправляє формат файлів, якщо щось не так. Цінність: жодна автоматизація "
            "не потрапляє в дашборд з порожньою чи битою карткою.",
        "changelog-generator": "Формує CHANGELOG і визначає наступну версію з git-комітів за "
            "Conventional Commits. Цінність: реліз-нотатки й версії більше не пишуться вручну і не "
            "розходяться з реальними змінами.",
        "clickup-task": "Оформлює вже обговорену роботу в задачу ClickUp: короткий опис і чекліст "
            "зробленого й того, що лишилось. Цінність: жодна домовленість не губиться між чатом і "
            "трекером задач.",
        "deep-research": "Проводить глибоке дослідження теми через OpenAI Deep Research API з пошуком "
            "в інтернеті. Цінність: розгорнутий аналіз замість короткої відповіді, коли питання того "
            "варте.",
        "doc-sync": "Пише або оновлює проєктну документацію, звіряючи її з реальним кодом і форматними "
            "правилами кожного типу файлу. Цінність: документація не розходиться з тим, що насправді є "
            "в проєкті.",
        "git-worktree-manager": "Організовує паралельну роботу над кількома фічами через git worktree "
            "— окремі гілки, порти, середовища. Цінність: кілька задач чи агентів працюють одночасно, "
            "не заважаючи один одному в тому самому репозиторії.",
        "humanizer": "Прибирає ознаки «написано ШІ» з тексту — канцелярит, зайві прикметники, штучні "
            "звороти. Цінність: текст, який дійсно читається як написаний людиною.",
        "skill-security-auditor": "Перевіряє скіл на небезпечний код (виконання команд, мережеві "
            "запити, спроби промпт-ін'єкції) перед тим, як його встановити. Цінність: сторонній скіл не "
            "стане способом непомітно щось зламати чи вкрасти дані.",
        "stage-1-supplax": "Створює повний стандартний набір документів для нового проєкту і вміє "
            "окремо перевіряти вже написані доки на прогалини й суперечності. Цінність: жоден проєкт не "
            "стартує без базової документації, і стара документація не лишається неперевіреною.",
        "tdd-guide": "Допомагає писати тести й вести розробку через них — Jest, Pytest, JUnit, Vitest, "
            "Mocha, включно з моками й аналізом покриття. Цінність: код одразу перевірений тестами, а "
            "не «колись потім».",
        "use-railway": "Керує інфраструктурою Railway: створення проєктів, сервісів, змінних, доменів, "
            "розгортання, діагностика падінь. Цінність: розгортання й адміністрування Railway без "
            "ручного клацання в UI.",
    }

    @app.cli.command("curate-skill-descriptions")
    def curate_skill_descriptions():
        """One-off data fix for skills pulled in via /skills/import-github's
        folder mode: strips a stray leading/trailing quote character off
        `name` left over from before github_sync.parse_skill_md stripped
        YAML quoting itself, and replaces the raw (often long, English)
        SKILL.md description with a short, value-focused Ukrainian one for
        the skills this project actually knows about. Safe to re-run - it
        only ever overwrites a name/description this command itself
        controls, and does nothing to a skill name it doesn't recognize.
        Whoever re-imports this folder from GitHub later will overwrite
        these back to the raw SKILL.md text, same as any other sync - this
        is a content curation pass, not a permanent override."""
        changed = 0
        for skill in Skill.query.all():
            cleaned_name = skill.name.strip("\"'") if skill.name else skill.name
            if cleaned_name != skill.name:
                skill.name = cleaned_name
                changed += 1
            uk_description = _SKILL_DESCRIPTIONS_UK.get(cleaned_name)
            if uk_description and skill.description != uk_description:
                skill.description = uk_description
                changed += 1
        db.session.commit()
        click.echo(f"Curated {changed} skill field(s).")

    @app.cli.command("create-user")
    @click.argument("email")
    @click.argument("name")
    @click.option("--admin", is_flag=True, help="Make this user an admin (default: viewer).")
    def create_user(email, name, admin):
        """Create a login account. Prompts for the password (hidden input) -
        never pass it as a command-line argument."""
        password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
        user = User(email=email.strip().lower(), name=name.strip(), role=Role.ADMIN if admin else Role.VIEWER,
                    is_confirmed=True, is_approved=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Created {user.role.value} user {user.email} (api_key: {user.api_key})")

    @app.cli.command("seed-demo")
    @click.argument("owner_email")
    def seed_demo(owner_email):
        """Seed the two real pilot automations found in ClickUp (Stage 0
        sales-forecast model, HR-бот) plus the stage-1-supplax skill entry,
        owned by an existing user. Run create-user first."""
        owner = User.query.filter_by(email=owner_email.strip().lower()).first()
        if not owner:
            click.echo(f"No user with email {owner_email} - run create-user first.")
            return

        def get_or_create_department(name, hue):
            dept = Department.query.filter_by(name=name).first()
            if not dept:
                dept = Department(name=name, hue=hue)
                db.session.add(dept)
            return dept

        sales = get_or_create_department("Sales", 275)
        hr = get_or_create_department("HR", 148)

        skill = Skill.query.filter_by(name="stage-1-supplax").first()
        if not skill:
            skill = Skill(
                name="stage-1-supplax",
                description="Бутстрапить новий проєкт повним набором документації й реальною структурою папок "
                             "за один прохід: README/ARCHITECTURE/ROI/PIPELINE, бібліотека довідників, і опційна "
                             "глибока перевірка документів кількома агентами.",
                when_to_use="На самому старті нового проєкту, або щоб перезапустити цикл перевірки документації "
                            "існуючого проєкту.",
            )
            db.session.add(skill)

        if not Automation.query.filter_by(slug="stage-0-forecast").first():
            forecast = Automation(
                slug="stage-0-forecast",
                name="Stage 0 — прогноз продажів",
                one_liner="Прогноз обсягу лідів, конверсії лід→угода та середнього чека по каналах.",
                status=Status.LIVE,
                owner=owner,
                departments=[sales],
                skills=[skill],
            )
            forecast.roi = ROIEntry(
                hypothesis="Точніший щомісячний прогноз DA$ дає керівництву час відреагувати на відхилення "
                            "раніше, ніж це видно по факту в кінці місяця.",
                metric_description="MAPE прогнозу DA$ проти факту, місяць до місяця.",
                confidence="measured",
                measured_value="9.8% MAPE",
            )
            db.session.add(forecast)

        if not Automation.query.filter_by(slug="hr-vacancy-bot").first():
            hr_bot = Automation(
                slug="hr-vacancy-bot",
                name="HR-бот моніторингу вакансій (@HRSupplaxBOT)",
                one_liner="Telegram-бот, який стежить за застряглими кандидатами й дедлайнами вакансій у PeopleForce.",
                status=Status.LIVE,
                owner=owner,
                departments=[hr],
                skills=[skill],
            )
            hr_bot.roi = ROIEntry(
                hypothesis="Рекрутери дізнаються про застряглого кандидата чи прострочену вакансію одразу, "
                            "а не під час ручної перевірки воронки раз на тиждень.",
                metric_description="Час між появою проблеми у воронці й реакцією рекрутера.",
                confidence="estimated",
            )
            db.session.add(hr_bot)

        db.session.commit()
        click.echo("Seeded departments, 2 pilot automations, and 1 skill entry.")


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
