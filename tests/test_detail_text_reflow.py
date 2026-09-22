"""Covers the `reflow` Jinja filter (src/app.py) and its use on the automation
detail page. The prose on that page is synced verbatim out of each repo's own
Markdown, which is hand-wrapped at ~75 characters; rendered under
`white-space: pre-line` those wraps became real line breaks and the text
stopped at about half the card's width on any wide screen."""
import re

from src.extensions import db
from src.models import Automation, AutomationPage, Role, User

WRAPPED = (
    "Раніше, щоб дашборд показував актуальний статус безпеки репозиторію,\n"
    "хтось вручну заходив у нього, запускав перевірку і сам вписував\n"
    "результат у файл."
)


def _csrf_token(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "expected a csrf_token hidden field in this page"
    return m.group(1)


def _make_user(email, name, role=Role.AUTOMATOR, password="pw12345"):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, email, password="pw12345"):
    token = _csrf_token(client.get("/login").get_data(as_text=True))
    client.post("/login", data={"email": email, "password": password, "csrf_token": token})


class TestReflowFilter:
    def _reflow(self, app):
        return app.jinja_env.filters["reflow"]

    def test_joins_hard_wrapped_lines_into_one_paragraph(self, app):
        out = self._reflow(app)(WRAPPED)
        assert "\n" not in out
        assert "репозиторію, хтось вручну" in out

    def test_keeps_blank_line_paragraph_breaks(self, app):
        out = self._reflow(app)("Перший рядок\nтого самого абзацу.\n\nДругий абзац.")
        assert out == "Перший рядок того самого абзацу.\n\nДругий абзац."

    def test_keeps_bullets_on_their_own_lines(self, app):
        out = self._reflow(app)("Ось список:\n- перший пункт\n- другий пункт")
        assert out == "Ось список:\n- перший пункт\n- другий пункт"

    def test_a_wrapped_bullet_still_joins_onto_its_own_line(self, app):
        out = self._reflow(app)("- дуже довгий пункт,\n  що переноситься\n- другий")
        assert out == "- дуже довгий пункт, що переноситься\n- другий"

    def test_empty_and_none_are_safe(self, app):
        assert self._reflow(app)(None) == ""
        assert self._reflow(app)("") == ""


class TestDetailPageRendersReflowed:
    def test_description_and_page_text_arrive_unwrapped(self, app, client):
        with app.app_context():
            owner = _make_user("o@x.com", "Owner")
            automation = Automation(slug="thing", name="Thing", owner_id=owner.id,
                                    description=WRAPPED)
            db.session.add(automation)
            db.session.flush()
            db.session.add(AutomationPage(automation_id=automation.id, name="Екран",
                                          description=WRAPPED, detail=WRAPPED))
            db.session.commit()

        _login(client, "o@x.com")
        html = client.get("/automations/thing").get_data(as_text=True)

        assert "репозиторію, хтось вручну" in html
        # the source's own wrap points must not survive into the markup
        assert "репозиторію,\nхтось" not in html
