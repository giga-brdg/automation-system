"""Covers dashboard/SECURITY_REVIEW.md sync for Skill (extends the same
mixin/format Automation uses - see src/models.py's SecurityReviewMixin).
Single-repo import (sync_skill_from_github) and the shared-repo folder bulk
import (sync_skills_from_github_folder) each fetch their own
{path/}dashboard/SECURITY_REVIEW.md."""
import re

from src import github_sync
from src.extensions import db
from src.models import Role, Skill, User


def _csrf_token(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "expected a csrf_token hidden field in this page"
    return m.group(1)


def _make_user(email, name, role=Role.AUTOMATOR):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=True)
    user.set_password("pw12345")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, email):
    token = _csrf_token(client.get("/login").get_data(as_text=True))
    client.post("/login", data={"email": email, "password": "pw12345", "csrf_token": token})


SKILL_MD = "---\nname: thing-skill\ndescription: does a thing\n---\n\nBody.\n"


class TestSingleSkillImportSecurityReview:
    def _stub(self, monkeypatch, security_review_text):
        def fake_fetch(owner, repo, path, branch):
            if path == "SKILL.md":
                return SKILL_MD
            if path == "dashboard/SECURITY_REVIEW.md":
                return security_review_text
            return None
        monkeypatch.setattr(github_sync, "fetch_raw_file", fake_fetch)
        monkeypatch.setattr(github_sync, "default_branch", lambda owner, repo: "main")

    def test_import_picks_up_a_clean_review(self, app, client, monkeypatch):
        self._stub(monkeypatch, "## Last Review\n2026-09-14\n\n## Open Findings\n- High: 0\n- Medium: 0\n")
        with app.app_context():
            _make_user("a@x.com", "A")
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/skills/import-github").get_data(as_text=True))
        client.post("/skills/import-github", data={
            "repo_url": "https://github.com/giga-brdg/thing-skill", "csrf_token": token,
        })
        with app.app_context():
            skill = Skill.query.filter_by(name="thing-skill").first()
            assert skill is not None
            assert skill.security_review_state == "clean"

    def test_import_without_the_file_stays_not_reviewed(self, app, client, monkeypatch):
        self._stub(monkeypatch, None)
        with app.app_context():
            _make_user("a@x.com", "A")
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/skills/import-github").get_data(as_text=True))
        client.post("/skills/import-github", data={
            "repo_url": "https://github.com/giga-brdg/thing-skill", "csrf_token": token,
        })
        with app.app_context():
            skill = Skill.query.filter_by(name="thing-skill").first()
            assert skill.security_review_state == "none"


class TestFolderBulkImportSecurityReview:
    def test_each_subdirectory_gets_its_own_security_review_file(self, app, client, monkeypatch):
        def fake_fetch(owner, repo, path, branch):
            if path == ".claude/skills/thing-skill/SKILL.md":
                return SKILL_MD
            if path == ".claude/skills/thing-skill/dashboard/SECURITY_REVIEW.md":
                return "## Last Review\n2026-09-14\n\n## Open Findings\n- High: 1\n- Medium: 0\n"
            if path == ".claude/skills/other-skill/SKILL.md":
                return "---\nname: other-skill\ndescription: another\n---\n"
            # other-skill has no dashboard/SECURITY_REVIEW.md at all
            return None
        monkeypatch.setattr(github_sync, "fetch_raw_file", fake_fetch)
        monkeypatch.setattr(github_sync, "default_branch", lambda owner, repo: "main")
        monkeypatch.setattr(github_sync, "list_directory", lambda owner, repo, path, branch: [
            {"name": "thing-skill", "type": "dir"},
            {"name": "other-skill", "type": "dir"},
        ])
        with app.app_context():
            _make_user("a@x.com", "A")
        _login(client, "a@x.com")
        token = _csrf_token(client.get("/skills/import-github").get_data(as_text=True))
        client.post("/skills/import-github", data={
            "repo_url": "https://github.com/giga-brdg/Skills_Supplax/tree/main/.claude/skills",
            "csrf_token": token,
        })
        with app.app_context():
            thing = Skill.query.filter_by(name="thing-skill").first()
            other = Skill.query.filter_by(name="other-skill").first()
            assert thing.security_review_state == "high"
            assert other.security_review_state == "none"
