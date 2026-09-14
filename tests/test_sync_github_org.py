"""Covers the `flask sync-github-org <owner>` CLI command (src/app.py) - the
bulk counterpart to /automations/import-github, meant to run on a schedule
(see check-token-usage for the same "one-shot, cron-invoked" convention).
Only repos with a dashboard/SUMMARY.md become automations here; archived
repos and repos without that file are skipped outright, not stubbed in."""
from src import github_sync
from src.extensions import db
from src.models import Automation, Role, User


def _make_user(email, name, role=Role.AUTOMATOR, is_approved=True):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=is_approved)
    user.set_password("pw12345")
    db.session.add(user)
    db.session.commit()
    return user


def _stub_org(monkeypatch, repos, summaries):
    """`repos`: list of {"name", "private", "archived", "default_branch"}.
    `summaries`: {repo_name: text_or_None} - what dashboard/SUMMARY.md
    returns for each."""
    monkeypatch.setattr(github_sync, "list_org_repos", lambda owner: repos)

    def fake_fetch(owner, repo, path, branch):
        if path == "dashboard/SUMMARY.md":
            return summaries.get(repo)
        if path == "README.md":
            return f"# {repo}\n\nSome repo.\n"
        return None
    monkeypatch.setattr(github_sync, "fetch_raw_file", fake_fetch)
    monkeypatch.setattr(github_sync, "default_branch", lambda owner, repo: "main")
    monkeypatch.setattr(github_sync, "fetch_latest_commit", lambda owner, repo, branch: None)


def _repo(name, archived=False, private=False):
    return {"name": name, "private": private, "archived": archived, "default_branch": "main"}


class TestSyncGithubOrg:
    def test_refuses_without_a_configured_owner(self, app, monkeypatch):
        monkeypatch.delenv("AUTOMATION_SYNC_OWNER_EMAIL", raising=False)
        with app.app_context():
            _stub_org(monkeypatch, [_repo("thing")], {"thing": "## Name\nThing\n"})
            runner = app.test_cli_runner()
            result = runner.invoke(args=["sync-github-org", "giga-brdg"])
            assert "AUTOMATION_SYNC_OWNER_EMAIL" in result.output
            assert Automation.query.count() == 0

    def test_imports_only_repos_with_summary_md(self, app, monkeypatch):
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "owner@x.com")
        with app.app_context():
            _stub_org(
                monkeypatch,
                [_repo("has-summary"), _repo("no-summary")],
                {"has-summary": "## Name\nHas Summary\n", "no-summary": None},
            )
            runner = app.test_cli_runner()
            result = runner.invoke(args=["sync-github-org", "giga-brdg"])
            assert "1 нових" in result.output
            assert "1 пропущено" in result.output
            slugs = {a.slug for a in Automation.query.all()}
            assert slugs == {"has-summary"}

    def test_skips_archived_repos(self, app, monkeypatch):
        with app.app_context():
            _make_user("owner@x.com", "Owner")
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "owner@x.com")
        with app.app_context():
            _stub_org(monkeypatch, [_repo("old-thing", archived=True)], {"old-thing": "## Name\nOld\n"})
            runner = app.test_cli_runner()
            runner.invoke(args=["sync-github-org", "giga-brdg"])
            assert Automation.query.count() == 0

    def test_existing_automation_keeps_its_current_owner(self, app, monkeypatch):
        with app.app_context():
            _make_user("default@x.com", "Default")
            real_owner = _make_user("real@x.com", "Real")
            real_owner_id = real_owner.id
            automation = Automation(slug="thing", name="Thing", owner_id=real_owner_id,
                                     repo_url="https://github.com/giga-brdg/thing")
            db.session.add(automation)
            db.session.commit()
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "default@x.com")
        with app.app_context():
            _stub_org(monkeypatch, [_repo("thing")], {"thing": "## Name\nThing\n"})
            runner = app.test_cli_runner()
            result = runner.invoke(args=["sync-github-org", "giga-brdg"])
            assert "1 оновлено" in result.output
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.owner_id == real_owner_id

    def test_a_brand_new_repo_is_owned_by_the_configured_default(self, app, monkeypatch):
        with app.app_context():
            default_owner = _make_user("default@x.com", "Default")
            default_owner_id = default_owner.id
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "default@x.com")
        with app.app_context():
            _stub_org(monkeypatch, [_repo("new-thing")], {"new-thing": "## Name\nNew Thing\n"})
            runner = app.test_cli_runner()
            runner.invoke(args=["sync-github-org", "giga-brdg"])
            automation = Automation.query.filter_by(slug="new-thing").first()
            assert automation is not None
            assert automation.owner_id == default_owner_id
