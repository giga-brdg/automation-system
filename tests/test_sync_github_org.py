"""Covers the `flask sync-github-org <owner>` CLI command (src/app.py) - the
bulk counterpart to /automations/import-github, meant to run on a schedule
(see check-token-usage for the same "one-shot, cron-invoked" convention).
Only repos with a dashboard/SUMMARY.md become real Automation rows; a
non-archived repo without one is tracked as PendingAutomation instead of
silently dropped (see TestPendingAutomations below) - only an archived repo
is skipped outright."""
import re

from src import github_sync
from src.extensions import db
from src.models import Automation, PendingAutomation, Role, User


def _make_user(email, name, role=Role.AUTOMATOR, is_approved=True):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=is_approved)
    user.set_password("pw12345")
    db.session.add(user)
    db.session.commit()
    return user


def _stub_org(monkeypatch, repos, summaries, pipelines=None):
    """`repos`: list of {"name", "private", "archived", "default_branch"}.
    `summaries`: {repo_name: text_or_None} - what dashboard/SUMMARY.md
    returns for each. `pipelines`: {repo_name: text_or_None} - what
    PIPELINE.md returns, checked only for a repo with no SUMMARY.md."""
    monkeypatch.setattr(github_sync, "list_org_repos", lambda owner: repos)
    pipelines = pipelines or {}

    def fake_fetch(owner, repo, path, branch):
        if path == "dashboard/SUMMARY.md":
            return summaries.get(repo)
        if path == "PIPELINE.md":
            return pipelines.get(repo)
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
            assert "1 неповних" in result.output
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

    def test_matches_an_existing_automation_by_repo_url_even_with_a_different_slug(self, app, monkeypatch):
        """Regression test: a hand-registered or single-repo-imported
        automation almost never has slug == repo-name-lowercased (a human
        picks their own slug) - matching on slug alone silently created a
        duplicate automation in production for exactly this case (repo
        'automation-system' already registered under slug
        'automation-dashboard') before this test was added."""
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
            automation = Automation(slug="my-custom-slug", name="Old Name", owner_id=owner.id,
                                     repo_url="https://github.com/giga-brdg/thing")
            db.session.add(automation)
            db.session.commit()
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "owner@x.com")
        with app.app_context():
            _stub_org(monkeypatch, [_repo("thing")], {"thing": "## Name\nNew Name\n"})
            runner = app.test_cli_runner()
            result = runner.invoke(args=["sync-github-org", "giga-brdg"])
            assert "0 нових" in result.output
            assert "1 оновлено" in result.output
            assert Automation.query.count() == 1
            automation = Automation.query.filter_by(slug="my-custom-slug").first()
            assert automation is not None
            assert automation.name == "New Name"

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


class TestPendingAutomations:
    """Any non-archived repo without dashboard/SUMMARY.md gets tracked as
    "known but incomplete" instead of silently skipped - see
    PendingAutomation in src/models.py. Whether PIPELINE.md is present only
    changes the `missing` text, not whether it's tracked at all: a real
    hand-rolled automation with no stage-0 history looks identical to an
    SDK package from repo content alone, so the distinction isn't reliable
    enough to silently drop a repo on."""

    def test_repo_with_pipeline_but_no_summary_becomes_pending(self, app, monkeypatch):
        with app.app_context():
            _make_user("owner@x.com", "Owner")
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "owner@x.com")
        with app.app_context():
            _stub_org(monkeypatch, [_repo("half-done")], summaries={}, pipelines={"half-done": "# Pipeline\n"})
            runner = app.test_cli_runner()
            result = runner.invoke(args=["sync-github-org", "giga-brdg"])
            assert "1 неповних" in result.output
            assert Automation.query.count() == 0
            pending = PendingAutomation.query.filter_by(slug="half-done").first()
            assert pending is not None
            assert pending.missing == (
                "dashboard/SUMMARY.md (стейдж-0 пройдено) — bootstrap почато (є PIPELINE.md), але не завершено."
            )
            assert pending.dismissed is False

    def test_repo_with_neither_file_is_still_tracked_as_pending(self, app, monkeypatch):
        """Regression test: a repo can be a real, hand-rolled automation
        (never bootstrapped via stage-1-supplax) - requiring PIPELINE.md as
        proof missed exactly this case in production (a real B2C product
        repo with no stage-0 history), so absence of PIPELINE.md must not
        silently drop the repo, only change the `missing` wording."""
        with app.app_context():
            _make_user("owner@x.com", "Owner")
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "owner@x.com")
        with app.app_context():
            _stub_org(monkeypatch, [_repo("maybe-an-automation")], summaries={}, pipelines={})
            runner = app.test_cli_runner()
            result = runner.invoke(args=["sync-github-org", "giga-brdg"])
            assert "1 неповних" in result.output
            pending = PendingAutomation.query.filter_by(slug="maybe-an-automation").first()
            assert pending is not None
            assert pending.missing == (
                "dashboard/SUMMARY.md (стейдж-0 ще не проходив) — bootstrap ще не починався (немає PIPELINE.md)."
            )

    def test_an_archived_repo_is_skipped_and_not_tracked(self, app, monkeypatch):
        with app.app_context():
            _make_user("owner@x.com", "Owner")
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "owner@x.com")
        with app.app_context():
            _stub_org(monkeypatch, [_repo("old-thing", archived=True)], summaries={}, pipelines={})
            runner = app.test_cli_runner()
            result = runner.invoke(args=["sync-github-org", "giga-brdg"])
            assert "1 архівованих пропущено" in result.output
            assert PendingAutomation.query.count() == 0

    def test_a_pending_repo_graduates_once_summary_appears(self, app, monkeypatch):
        with app.app_context():
            owner = _make_user("owner@x.com", "Owner")
            pending = PendingAutomation(slug="half-done", name="half-done",
                                         repo_url="https://github.com/giga-brdg/half-done",
                                         missing="dashboard/SUMMARY.md")
            db.session.add(pending)
            db.session.commit()
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "owner@x.com")
        with app.app_context():
            _stub_org(monkeypatch, [_repo("half-done")], {"half-done": "## Name\nHalf Done\n"})
            runner = app.test_cli_runner()
            runner.invoke(args=["sync-github-org", "giga-brdg"])
            assert PendingAutomation.query.count() == 0
            assert Automation.query.filter_by(slug="half-done").first() is not None

    def test_dismissed_pending_is_not_recreated_by_a_later_run(self, app, monkeypatch):
        with app.app_context():
            _make_user("owner@x.com", "Owner")
            pending = PendingAutomation(slug="half-done", name="half-done",
                                         repo_url="https://github.com/giga-brdg/half-done",
                                         missing="dashboard/SUMMARY.md", dismissed=True)
            db.session.add(pending)
            db.session.commit()
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "owner@x.com")
        with app.app_context():
            _stub_org(monkeypatch, [_repo("half-done")], summaries={}, pipelines={"half-done": "# Pipeline\n"})
            runner = app.test_cli_runner()
            runner.invoke(args=["sync-github-org", "giga-brdg"])
            pending = PendingAutomation.query.filter_by(slug="half-done").first()
            assert pending.dismissed is True


class TestPendingAutomationDismiss:
    def test_automator_can_dismiss(self, app, client, monkeypatch):
        import re

        with app.app_context():
            user = _make_user("owner@x.com", "Owner")
            pending = PendingAutomation(slug="half-done", name="half-done",
                                         repo_url="https://github.com/giga-brdg/half-done",
                                         missing="dashboard/SUMMARY.md")
            db.session.add(pending)
            db.session.commit()
            pending_id = pending.id
        token_html = client.get("/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', token_html).group(1)
        client.post("/login", data={"email": "owner@x.com", "password": "pw12345", "csrf_token": token})
        html = client.get("/automations").get_data(as_text=True)
        assert "half-done" in html
        ptoken = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        resp = client.post(f"/automations/pending/{pending_id}/dismiss", data={"csrf_token": ptoken})
        assert resp.status_code == 302
        with app.app_context():
            assert PendingAutomation.query.get(pending_id).dismissed is True
        # Checks the dismissed row's own per-id action URL is gone, not the
        # automation's name - the success flash re-displayed on this same
        # request also mentions the name, which would make a name-based
        # assertion pass vacuously.
        html = client.get("/automations").get_data(as_text=True)
        assert f"/automations/pending/{pending_id}/dismiss" not in html

    def test_a_viewer_cannot_dismiss(self, app, client, monkeypatch):
        import re

        with app.app_context():
            _make_user("viewer@x.com", "Viewer", role=Role.VIEWER)
            pending = PendingAutomation(slug="half-done", name="half-done",
                                         repo_url="https://github.com/giga-brdg/half-done",
                                         missing="dashboard/SUMMARY.md")
            db.session.add(pending)
            db.session.commit()
            pending_id = pending.id
        token_html = client.get("/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', token_html).group(1)
        client.post("/login", data={"email": "viewer@x.com", "password": "pw12345", "csrf_token": token})
        resp = client.post(f"/automations/pending/{pending_id}/dismiss", data={"csrf_token": token})
        assert resp.status_code == 403

    def test_a_viewer_does_not_see_the_pending_block_at_all(self, app, client, monkeypatch):
        """Design audit, Critical #3: the pending-sync block used to render
        for every role (only its dismiss button was gated), so a Viewer saw
        a wall of internal onboarding debris with no action available on it.
        The whole block is now gated the same way as the button."""
        import re

        with app.app_context():
            _make_user("viewer@x.com", "Viewer", role=Role.VIEWER)
            db.session.add(PendingAutomation(slug="half-done", name="half-done",
                                              repo_url="https://github.com/giga-brdg/half-done",
                                              missing="dashboard/SUMMARY.md"))
            db.session.commit()
        token_html = client.get("/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', token_html).group(1)
        client.post("/login", data={"email": "viewer@x.com", "password": "pw12345", "csrf_token": token})
        html = client.get("/automations").get_data(as_text=True)
        assert "half-done" not in html
        assert "Виявлено в GitHub" not in html


class TestSyncGithubOrgButton:
    """The "Оновити з GitHub" button on /automations - runs the same scan as
    the cron-invoked CLI command, but on demand via POST /automations/sync-
    github-org, for whoever doesn't want to wait for the daily 03:00 UTC
    run."""

    def _login(self, client, email):
        token_html = client.get("/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', token_html).group(1)
        client.post("/login", data={"email": email, "password": "pw12345", "csrf_token": token})

    def test_automator_can_trigger_a_sync(self, app, client, monkeypatch):
        with app.app_context():
            _make_user("owner@x.com", "Owner")
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "owner@x.com")
        monkeypatch.setenv("GITHUB_SYNC_ORG", "giga-brdg")
        with app.app_context():
            _stub_org(monkeypatch, [_repo("thing")], {"thing": "## Name\nThing\n"})
        self._login(client, "owner@x.com")
        html = client.get("/automations").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        resp = client.post("/automations/sync-github-org", data={"csrf_token": token}, follow_redirects=True)
        assert resp.status_code == 200
        assert "1 нових" in resp.get_data(as_text=True)
        with app.app_context():
            assert Automation.query.filter_by(slug="thing").count() == 1

    def test_a_viewer_cannot_trigger_a_sync(self, app, client, monkeypatch):
        with app.app_context():
            _make_user("viewer@x.com", "Viewer", role=Role.VIEWER)
        monkeypatch.setenv("GITHUB_SYNC_ORG", "giga-brdg")
        # A viewer never sees the button/its csrf-carrying form (/automations
        # renders none for them), so grab a token from the login page before
        # logging in - same as TestPendingAutomationDismiss's viewer test.
        token_html = client.get("/login").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', token_html).group(1)
        self._login(client, "viewer@x.com")
        resp = client.post("/automations/sync-github-org", data={"csrf_token": token})
        assert resp.status_code == 403

    def test_refuses_without_a_configured_org(self, app, client, monkeypatch):
        with app.app_context():
            _make_user("owner@x.com", "Owner")
        monkeypatch.delenv("GITHUB_SYNC_ORG", raising=False)
        self._login(client, "owner@x.com")
        html = client.get("/automations").get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        resp = client.post("/automations/sync-github-org", data={"csrf_token": token}, follow_redirects=True)
        assert "GITHUB_SYNC_ORG" in resp.get_data(as_text=True)


class TestSyncGithubOrgRobustness:
    """Covers run_github_org_sync's concurrency guard and its per-repo (not
    whole-scan) exception handling, added after a code review found the
    original except-Exception scope wrapped the entire loop - a failure on
    any one repo used to be misreported as "couldn't list repos" even
    though listing succeeded and earlier repos were already committed."""

    def test_a_second_concurrent_call_is_refused_not_queued(self, app):
        from src.app import _github_org_sync_lock, run_github_org_sync

        with app.app_context():
            _make_user("owner@x.com", "Owner")
        assert _github_org_sync_lock.acquire(blocking=False)
        try:
            with app.app_context():
                try:
                    run_github_org_sync(app, "giga-brdg")
                    assert False, "expected a ValueError while the lock is already held"
                except ValueError as e:
                    assert "вже виконується" in str(e)
        finally:
            _github_org_sync_lock.release()

    def test_a_failure_on_one_repo_does_not_abort_the_scan_or_earlier_commits(self, app, monkeypatch):
        from src.app import run_github_org_sync

        with app.app_context():
            _make_user("owner@x.com", "Owner")
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "owner@x.com")
        with app.app_context():
            _stub_org(
                monkeypatch,
                [_repo("first-ok"), _repo("second-broken"), _repo("third-ok")],
                {"first-ok": "## Name\nFirst\n", "second-broken": "## Name\nBroken\n",
                 "third-ok": "## Name\nThird\n"},
            )
            real_fetch_latest_commit = github_sync.fetch_latest_commit

            def flaky_fetch_latest_commit(owner, repo, branch):
                if repo == "second-broken":
                    raise RuntimeError("simulated transient GitHub error")
                return real_fetch_latest_commit(owner, repo, branch)
            monkeypatch.setattr(github_sync, "fetch_latest_commit", flaky_fetch_latest_commit)

            summary = run_github_org_sync(app, "giga-brdg")
            # The two good repos are committed and counted even though the
            # middle one blew up - a whole-scan except would have lost both
            # the commits already made and reported a misleading "couldn't
            # list repos" instead of this per-repo failure count.
            assert "2 нових" in summary
            assert "1 репозиторіїв пропущено через помилку" in summary
            slugs = {a.slug for a in Automation.query.all()}
            assert slugs == {"first-ok", "third-ok"}

    def test_cli_owner_defaults_to_github_sync_org_env_var(self, app, monkeypatch):
        with app.app_context():
            _make_user("owner@x.com", "Owner")
        monkeypatch.setenv("AUTOMATION_SYNC_OWNER_EMAIL", "owner@x.com")
        monkeypatch.setenv("GITHUB_SYNC_ORG", "giga-brdg")
        with app.app_context():
            _stub_org(monkeypatch, [_repo("thing")], {"thing": "## Name\nThing\n"})
            runner = app.test_cli_runner()
            result = runner.invoke(args=["sync-github-org"])
            assert "1 нових" in result.output
