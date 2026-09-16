"""Covers _flash.html's category-to-Bootstrap-alert-variant mapping, added
during the Able Pro redesign (see ARCHITECTURE.md/DEPLOYMENT.md). Every
flash() call in src/app.py now passes an explicit category; this just checks
a representative sample renders the right `alert-*` class, not every call
site."""
import re

from src.extensions import db
from src.models import Department, Role, User


def _csrf_token(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "expected a csrf_token hidden field in this page"
    return m.group(1)


def _make_user(email, name, role=Role.VIEWER, is_confirmed=True, is_approved=True, password="pw12345"):
    user = User(email=email, name=name, role=role, is_confirmed=is_confirmed, is_approved=is_approved)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


class TestFlashCategoryRendering:
    def test_error_flash_renders_as_alert_danger(self, app, client):
        # Wrong password -> flash(..., "error") in the login route.
        token = _csrf_token(client.get("/login").get_data(as_text=True))
        html = client.post("/login", data={"email": "nobody@x.com", "password": "wrong", "csrf_token": token},
                            follow_redirects=True).get_data(as_text=True)
        assert "alert-danger" in html

    def test_success_flash_renders_as_alert_success(self, app, client):
        with app.app_context():
            _make_user("admin@x.com", "Admin", role=Role.ADMIN)
            from_dept = Department(name="Продажі", hue=10)
            into_dept = Department(name="Маркетинг", hue=20)
            db.session.add_all([from_dept, into_dept])
            db.session.commit()
            from_id, into_id = from_dept.id, into_dept.id

        login_token = _csrf_token(client.get("/login").get_data(as_text=True))
        client.post("/login", data={"email": "admin@x.com", "password": "pw12345", "csrf_token": login_token})
        token = _csrf_token(client.get("/departments").get_data(as_text=True))
        html = client.post("/departments/merge",
                            data={"csrf_token": token, "from_id": from_id, "into_id": into_id},
                            follow_redirects=True).get_data(as_text=True)
        assert "alert-success" in html

    def test_uncategorized_flash_falls_back_to_warning_not_danger(self, app):
        # _flash.html's default for any category not in its map (including
        # Flask's own implicit "message" category, from a bare flash(msg)
        # call with no category argument) is "warning", not "danger" - an
        # uncategorized message shouldn't read as an error.
        from flask import flash, render_template

        with app.test_request_context("/login"):
            flash("no category given")
            html = render_template("_flash.html")
        assert "alert-warning" in html
        assert "alert-danger" not in html
