"""Covers who an automation belongs to and who may change that: the admin-only
transfer control on the automation page, and the GitHub login the org scan
matches a repo's top contributor against (src/app.py's _contributor_owner).
Owning an automation is what User.can_manage grants edit/resync rights from,
so both routes are really permission changes wearing a different name."""
import re

from src.extensions import db
from src.models import Automation, Role, User


def _csrf_token(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "expected a csrf_token hidden field in this page"
    return m.group(1)


def _make_user(email, name, role=Role.AUTOMATOR, github_username=None):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=True,
                github_username=github_username)
    user.set_password("pw12345")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, email):
    token = _csrf_token(client.get("/login").get_data(as_text=True))
    client.post("/login", data={"email": email, "password": "pw12345", "csrf_token": token})


def _automation(owner, slug="thing"):
    automation = Automation(slug=slug, name="Thing", owner_id=owner.id)
    db.session.add(automation)
    db.session.commit()
    return automation


def _post(client, url, **fields):
    token = _csrf_token(client.get("/automations/thing").get_data(as_text=True))
    return client.post(url, data={"csrf_token": token, **fields}, follow_redirects=True)


class TestTransferOwner:
    def test_admin_can_hand_an_automation_to_another_automator(self, app, client):
        with app.app_context():
            admin = _make_user("admin@x.com", "Admin", role=Role.ADMIN)
            _automation(admin)
            new_owner_id = _make_user("dev@x.com", "Dev").id

        _login(client, "admin@x.com")
        _post(client, "/automations/thing/transfer", owner_id=new_owner_id)

        with app.app_context():
            assert Automation.query.filter_by(slug="thing").first().owner.email == "dev@x.com"

    def test_owning_automator_cannot_transfer_their_own_automation(self, app, client):
        with app.app_context():
            owner = _make_user("dev@x.com", "Dev")
            _automation(owner)
            owner_id = owner.id
            other_id = _make_user("other@x.com", "Other").id

        _login(client, "dev@x.com")
        # can_manage lets them edit it; handing it away is still admin-only
        token = _csrf_token(client.get(f"/automators/{owner_id}").get_data(as_text=True))
        resp = client.post("/automations/thing/transfer",
                           data={"csrf_token": token, "owner_id": other_id})
        assert resp.status_code == 403

        with app.app_context():
            assert Automation.query.filter_by(slug="thing").first().owner.email == "dev@x.com"

    def test_a_viewer_cannot_be_made_an_owner(self, app, client):
        with app.app_context():
            admin = _make_user("admin@x.com", "Admin", role=Role.ADMIN)
            _automation(admin)
            viewer_id = _make_user("watcher@x.com", "Watcher", role=Role.VIEWER).id

        _login(client, "admin@x.com")
        html = _post(client, "/automations/thing/transfer", owner_id=viewer_id).get_data(as_text=True)
        assert "лише Automator або Admin" in html

        with app.app_context():
            assert Automation.query.filter_by(slug="thing").first().owner.email == "admin@x.com"


class TestGithubUsername:
    def test_admin_can_set_a_login(self, app, client):
        with app.app_context():
            _make_user("admin@x.com", "Admin", role=Role.ADMIN)
            dev_id = _make_user("dev@x.com", "Dev").id

        _login(client, "admin@x.com")
        token = _csrf_token(client.get(f"/automators/{dev_id}").get_data(as_text=True))
        client.post(f"/automators/{dev_id}/github-username",
                    data={"csrf_token": token, "github_username": "@DevLogin"},
                    follow_redirects=True)

        with app.app_context():
            # a pasted "@handle" is the same handle
            assert db.session.get(User, dev_id).github_username == "DevLogin"

    def test_the_same_login_cannot_point_at_two_people(self, app, client):
        with app.app_context():
            _make_user("admin@x.com", "Admin", role=Role.ADMIN)
            _make_user("first@x.com", "First", github_username="shared")
            second_id = _make_user("second@x.com", "Second").id

        _login(client, "admin@x.com")
        token = _csrf_token(client.get(f"/automators/{second_id}").get_data(as_text=True))
        html = client.post(f"/automators/{second_id}/github-username",
                           data={"csrf_token": token, "github_username": "SHARED"},
                           follow_redirects=True).get_data(as_text=True)
        assert "уже закріплений" in html

        with app.app_context():
            assert db.session.get(User, second_id).github_username is None

    def test_an_automator_cannot_set_their_own_login(self, app, client):
        with app.app_context():
            dev_id = _make_user("dev@x.com", "Dev").id

        _login(client, "dev@x.com")
        token = _csrf_token(client.get(f"/automators/{dev_id}").get_data(as_text=True))
        resp = client.post(f"/automators/{dev_id}/github-username",
                           data={"csrf_token": token, "github_username": "devlogin"})
        assert resp.status_code == 403

        with app.app_context():
            assert db.session.get(User, dev_id).github_username is None
