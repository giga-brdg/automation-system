import io
import os
import re
import secrets
import zipfile
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import click
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required, login_user, logout_user

from . import github_sync, telegram
from .extensions import db, login_manager
from .models import (
    Automation,
    AutomationPage,
    AutomationTodoItem,
    Comparison,
    Connection,
    Department,
    FeatureRow,
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


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")

    default_db = f"sqlite:///{(BASE_DIR / 'data' / 'portfolio.db').as_posix()}"
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL") or default_db
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.environ.get("AUTH_SECRET") or "dev-only-insecure-key-set-AUTH_SECRET-in-.env"

    db.init_app(app)
    login_manager.init_app(app)

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


def register_routes(app):
    @app.route("/")
    def index():
        return redirect(url_for("automations_list"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = User.query.filter_by(email=email).first()
            if user and user.check_password(password):
                if not user.is_confirmed:
                    flash("Спочатку підтверди код, який тобі назве адміністратор.")
                    return redirect(url_for("confirm", email=email))
                if not user.is_approved:
                    flash("Код підтверджено, але адміністратор ще не надав доступ у Telegram-боті.")
                    return render_template("login.html")
                login_user(user)
                return redirect(url_for("automations_list"))
            flash("Невірний email або пароль.")
        return render_template("login.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            name = request.form.get("name", "").strip()
            password = request.form.get("password", "")
            password_confirm = request.form.get("password_confirm", "")
            if not email or not name or not password:
                flash("Заповни всі поля.")
                return render_template("register.html")
            if password != password_confirm:
                flash("Паролі не збігаються.")
                return render_template("register.html")

            existing = User.query.filter_by(email=email).first()
            if existing and (existing.is_confirmed or existing.is_approved):
                flash("Акаунт з такою поштою вже існує — увійди звичайним способом.")
                return render_template("register.html")

            user = existing or User(email=email, role=Role.VIEWER)
            user.name = name
            user.set_password(password)
            user.pending_code = f"{secrets.randbelow(1_000_000):06d}"
            user.pending_code_expires_at = _now().replace(tzinfo=None) + timedelta(minutes=30)
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
                      "звернись до нього напряму.")
            else:
                flash("Реєстрацію подано. Введи код підтвердження, який тобі назве адміністратор.")
            return redirect(url_for("confirm", email=email))
        return render_template("register.html")

    @app.route("/confirm", methods=["GET", "POST"])
    def confirm():
        email = request.values.get("email", "").strip().lower()
        if request.method == "POST":
            code = request.form.get("code", "").strip()
            user = User.query.filter_by(email=email).first()
            if not user or not user.pending_code:
                flash("Акаунт не знайдено, або код уже використано.")
            elif user.pending_code_expires_at and user.pending_code_expires_at < _now().replace(tzinfo=None):
                flash("Код застарів — попроси адміністратора зареєструвати тебе ще раз.")
            elif code != user.pending_code:
                flash("Невірний код.")
            else:
                user.is_confirmed = True
                user.pending_code = None
                user.pending_code_expires_at = None
                db.session.commit()
                flash("Акаунт підтверджено. Очікуй, поки адміністратор надасть доступ у Telegram-боті.")
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
        return render_template(
            "automations_list.html",
            automations=automations,
            statuses=Status,
            counts=counts,
            departments=departments,
            active_status=status_filter,
            active_department=dept_filter,
            search=search,
        )

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
        automation.repo_url = form.get("repo_url", "").strip() or None
        automation.clickup_url = form.get("clickup_url", "").strip() or None
        automation.departments = Department.query.filter(
            Department.id.in_(form.getlist("departments"))).all()
        automation.skills = Skill.query.filter(Skill.id.in_(form.getlist("skills"))).all()
        if automation.roi is None:
            automation.roi = ROIEntry()
        automation.roi.hypothesis = form.get("hypothesis", "").strip()
        automation.roi.metric_description = form.get("metric_description", "").strip()
        automation.roi.confidence = form.get("confidence", "estimated")
        automation.roi.presentation_url = form.get("presentation_url", "").strip() or None

    @app.route("/automations/new", methods=["GET", "POST"])
    @login_required
    @automator_required
    def automation_new():
        users = User.query.order_by(User.name).all()
        departments = Department.query.order_by(Department.name).all()
        skills = Skill.query.order_by(Skill.name).all()
        if request.method == "POST":
            automation = Automation(slug=request.form["slug"].strip(), owner_id=current_user.id)
            apply_manual_form(automation, request.form)
            db.session.add(automation)
            db.session.commit()
            return redirect(url_for("automation_detail", slug=automation.slug))
        return render_template("automation_form.html", departments=departments, skills=skills,
                                users=users, statuses=Status, automation=None)

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
            apply_manual_form(automation, request.form)
            db.session.commit()
            return redirect(url_for("automation_detail", slug=automation.slug))
        return render_template("automation_form.html", departments=departments, skills=skills,
                                users=users, statuses=Status, automation=automation)

    def sync_automation_from_github(automation, repo_url, owner_id, form_status, selected_dept_ids, slug=None):
        """Shared by the first-time import form and the per-automation
        'Оновити з GitHub' button: fetch README.md/dashboard/ROI.md/
        dashboard/SUMMARY.md and apply them to `automation` (a new unsaved
        instance, or an existing one being refreshed). These two live in a
        dedicated dashboard/ folder because they're a generated sync
        contract, not hand-maintained project docs - the automation's repo
        keeps its own real ROI/functionality writeups elsewhere (docs/
        roi_explained.md, docs/functions.md) and regenerates these two from
        them. Raises on a GitHub fetch failure - callers turn that into a
        flash message."""
        parsed = github_sync.parse_repo_url(repo_url)
        if not parsed:
            raise ValueError("Не схоже на посилання на GitHub-репозиторій "
                              "(очікую https://github.com/власник/репо).")
        owner_gh, repo = parsed

        branch = github_sync.default_branch(owner_gh, repo)
        readme_text = github_sync.fetch_raw_file(owner_gh, repo, "README.md", branch)
        roi_text = github_sync.fetch_raw_file(owner_gh, repo, "dashboard/ROI.md", branch)
        summary_text = github_sync.fetch_raw_file(owner_gh, repo, "dashboard/SUMMARY.md", branch)
        functions_text = github_sync.fetch_raw_file(owner_gh, repo, "dashboard/functions.md", branch)
        backlog_text = github_sync.fetch_raw_file(owner_gh, repo, "backlog/BACKLOG.md", branch)
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
            automation.roi.presentation_url = fields["presentation_url"] or automation.roi.presentation_url
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

        if todo_text is not None:
            todo_items = github_sync.parse_todo_md(todo_text)
            automation.todo_items = [
                AutomationTodoItem(text=t["text"], done=t["done"], order_index=i)
                for i, t in enumerate(todo_items)
            ]

        return automation, warnings

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
                flash("Ця автоматизація вже зареєстрована іншим автоматизатором.")
                return redirect(url_for("automation_import_github"))
            owner_id = int(request.form["owner_id"]) if current_user.is_admin else current_user.id
            try:
                automation, warnings = sync_automation_from_github(
                    existing, repo_url, owner_id,
                    request.form.get("status") or "", request.form.getlist("departments"), slug=slug)
            except ValueError as e:
                flash(str(e))
                return redirect(url_for("automation_import_github"))
            except Exception:
                db.session.rollback()
                app.logger.exception("GitHub import failed for %s", repo_url)
                flash("Не вдалося звернутися до GitHub — перевір посилання і чи репозиторій публічний "
                      "(або що GITHUB_TOKEN в .env дійсний, якщо приватний).")
                return redirect(url_for("automation_import_github"))

            db.session.commit()
            flash(f"Синхронізовано з {repo_url}." + (" " + " ".join(warnings) if warnings else ""))
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
            flash("У цієї автоматизації не вказано посилання на репозиторій.")
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
            flash("Не вдалося звернутися до GitHub — перевір, чи репозиторій усе ще доступний.")
            return redirect(url_for("automation_detail", slug=slug))

        db.session.commit()
        flash("Оновлено з GitHub." + (" " + " ".join(warnings) if warnings else ""))
        return redirect(url_for("automation_detail", slug=automation.slug))

    @app.route("/automations/<slug>")
    @login_required
    def automation_detail(slug):
        automation = Automation.query.filter_by(slug=slug).first_or_404()
        return render_template("automation_detail.html", automation=automation)

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
            flash("Назва відділу не може бути порожньою.")
        elif Department.query.filter(Department.name == new_name, Department.id != dept_id).first():
            flash(f"Відділ з назвою «{new_name}» вже існує — злий їх через об'єднання нижче, а не перейменування.")
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
            flash("Неможливо об'єднати відділ сам із собою.")
            return redirect(url_for("departments_list"))
        for automation in list(from_dept.automations):
            if into_dept not in automation.departments:
                automation.departments.append(into_dept)
            automation.departments.remove(from_dept)
        db.session.delete(from_dept)
        db.session.commit()
        flash(f"«{from_dept.name}» об'єднано з «{into_dept.name}».")
        return redirect(url_for("departments_list"))

    @app.route("/departments/<int:dept_id>/delete", methods=["POST"])
    @login_required
    @admin_required
    def department_delete(dept_id):
        dept = Department.query.get_or_404(dept_id)
        if dept.automations:
            flash(f"«{dept.name}» використовується у {len(dept.automations)} автоматизаціях — "
                  f"спочатку об'єднай з іншим відділом, потім видаляй.")
        else:
            db.session.delete(dept)
            db.session.commit()
        return redirect(url_for("departments_list"))

    @app.route("/automators/<int:user_id>")
    @login_required
    def automator_profile(user_id):
        automator = User.query.get_or_404(user_id)
        return render_template("automator_profile.html", automator=automator)

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
            flash("Не вдалося завантажити файли скіла з GitHub.")
            return redirect(url_for("skills_library"))
        if not files:
            flash("У цьому скілі не знайдено файлів для завантаження.")
            return redirect(url_for("skills_library"))

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel_path, content in files.items():
                zf.writestr(rel_path, content)
        buf.seek(0)
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", skill.name).strip("-") or "skill"
        return send_file(buf, mimetype="application/zip", as_attachment=True,
                          download_name=f"{safe_name}.zip")

    def _upsert_skill(name, description, repo_url, doc_url):
        skill = Skill.query.filter_by(name=name).first()
        if skill is None:
            skill = Skill(name=name)
            db.session.add(skill)
        skill.description = description or skill.description
        skill.repo_url = repo_url
        skill.doc_url = doc_url
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

        return _upsert_skill(fields["name"], fields.get("description"), repo_url,
                              f"https://github.com/{owner_gh}/{repo}/blob/{branch}/{skill_path}")

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
            _upsert_skill(fields["name"], fields.get("description"),
                          f"https://github.com/{owner_gh}/{repo}/tree/{branch}/{path}/{name}",
                          f"https://github.com/{owner_gh}/{repo}/blob/{branch}/{skill_path}")
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
                flash(str(e))
                return redirect(url_for("skill_import_github"))
            except Exception:
                db.session.rollback()
                app.logger.exception("GitHub import failed for skill(s) %s", repo_url)
                flash("Не вдалося звернутися до GitHub — перевір посилання і чи репозиторій публічний "
                      "(або що GITHUB_TOKEN в .env дійсний, якщо приватний).")
                return redirect(url_for("skill_import_github"))

            db.session.commit()
            if is_folder:
                if imported:
                    msg = f"Імпортовано {len(imported)} скіл(ів): {', '.join(imported)}."
                else:
                    msg = "Жодного скіла не імпортовано."
                if skipped:
                    msg += " Пропущено: " + ", ".join(skipped) + "."
                flash(msg)
            else:
                flash(f"Синхронізовано скіл «{skill.name}» з {repo_url}.")
            return redirect(url_for("skills_library"))
        return render_template("skill_import.html")

    @app.route("/api/automations/<slug>/sync", methods=["POST"])
    def api_sync_automation(slug):
        """Machine-facing endpoint for stage-0-supplax's portfolio-sync step to
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
        if owner.role not in (Role.ADMIN, Role.AUTOMATOR):
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
        automation.repo_url = payload.get("repo_url", automation.repo_url)
        automation.clickup_url = payload.get("clickup_url", automation.clickup_url)
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
            automation.roi.presentation_url = roi.get("presentation_url", automation.roi.presentation_url)

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
        "stage-0-supplax": "Створює повний стандартний набір документів для нового проєкту і вміє "
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
        sales-forecast model, HR-бот) plus the stage-0-supplax skill entry,
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

        skill = Skill.query.filter_by(name="stage-0-supplax").first()
        if not skill:
            skill = Skill(
                name="stage-0-supplax",
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
    app.run(debug=True)
