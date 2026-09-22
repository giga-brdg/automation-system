"""Covers the cost side of ROI: ROIEntry.dev_hours/maintenance_hours_per_month
and their best_hours_saved_per_month/net_hours_per_month properties, the new
Subscription model and its even-split cost_per_automation/
Automation.monthly_subscription_cost_usd, saving both through
/automations/<slug>/edit (apply_manual_form), and the /subscriptions admin
routes (src/app.py)."""
import re

from src.extensions import db
from src.models import Automation, ROIEntry, Role, Subscription, User


def _csrf_token(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "expected a csrf_token hidden field in this page"
    return m.group(1)


def _make_user(email, name, role=Role.AUTOMATOR, is_approved=True, password="pw12345"):
    user = User(email=email, name=name, role=role, is_confirmed=True, is_approved=is_approved)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, email, password="pw12345"):
    token = _csrf_token(client.get("/login").get_data(as_text=True))
    client.post("/login", data={"email": email, "password": password, "csrf_token": token})


class TestROIEntryCostProperties:
    def test_best_hours_saved_prefers_measured_over_estimated(self, app):
        with app.app_context():
            roi = ROIEntry(
                baseline_cycle_minutes=45, baseline_frequency_per_month=12,
                target_cycle_minutes=5, target_frequency_per_month=12,
                measured_hours_per_month=3,
            )
            assert roi.estimated_hours_saved_per_month == 8  # 9 - 1
            assert roi.best_hours_saved_per_month == 3  # measured wins

    def test_best_hours_saved_falls_back_to_estimate_when_unmeasured(self, app):
        with app.app_context():
            roi = ROIEntry(
                baseline_cycle_minutes=45, baseline_frequency_per_month=12,
                target_cycle_minutes=5, target_frequency_per_month=12,
            )
            assert roi.best_hours_saved_per_month == 8

    def test_net_hours_needs_both_saved_and_maintenance(self, app):
        with app.app_context():
            roi = ROIEntry(measured_hours_per_month=10)
            assert roi.net_hours_per_month is None  # no maintenance figure yet
            roi.maintenance_hours_per_month = 4
            assert roi.net_hours_per_month == 6

    def test_net_hours_excludes_dev_hours(self, app):
        """dev_hours is one-time, not monthly - folding it into net_hours_per_month
        would misrepresent it as recurring, so it must never affect this figure."""
        with app.app_context():
            roi = ROIEntry(measured_hours_per_month=10, maintenance_hours_per_month=4, dev_hours=999)
            assert roi.net_hours_per_month == 6

    def test_net_hours_can_be_negative(self, app):
        with app.app_context():
            roi = ROIEntry(measured_hours_per_month=2, maintenance_hours_per_month=5)
            assert roi.net_hours_per_month == -3


class TestSubscriptionAllocation:
    def test_cost_per_automation_is_none_when_unlinked(self, app):
        with app.app_context():
            sub = Subscription(name="Claude Code", monthly_cost_usd=30)
            db.session.add(sub)
            db.session.commit()
            assert sub.cost_per_automation is None
            assert Automation is not None  # sanity: import used below too

    def test_cost_per_automation_splits_evenly(self, app):
        with app.app_context():
            owner = _make_user("o@x.com", "O")
            a1 = Automation(slug="a1", name="A1", owner_id=owner.id)
            a2 = Automation(slug="a2", name="A2", owner_id=owner.id)
            sub = Subscription(name="VPS", monthly_cost_usd=100, automations=[a1, a2])
            db.session.add_all([a1, a2, sub])
            db.session.commit()
            assert float(sub.cost_per_automation) == 50

            assert float(a1.monthly_subscription_cost_usd) == 50
            assert float(a2.monthly_subscription_cost_usd) == 50

    def test_automation_with_no_subscriptions_has_none_cost(self, app):
        with app.app_context():
            owner = _make_user("o@x.com", "O")
            a1 = Automation(slug="a1", name="A1", owner_id=owner.id)
            db.session.add(a1)
            db.session.commit()
            assert a1.monthly_subscription_cost_usd is None


class TestEditFormSavesCostFields:
    def test_dev_and_maintenance_hours_saved(self, app, client):
        with app.app_context():
            user = _make_user("owner@x.com", "Owner")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            db.session.add(automation)
            db.session.commit()
        _login(client, "owner@x.com")
        token = _csrf_token(client.get("/automations/thing/edit").get_data(as_text=True))
        resp = client.post("/automations/thing/edit", data={
            "csrf_token": token, "name": "Thing", "status": "live",
            "dev_hours": "40", "maintenance_hours_per_month": "2.5",
        })
        assert resp.status_code == 302
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert float(automation.roi.dev_hours) == 40
            assert float(automation.roi.maintenance_hours_per_month) == 2.5

    def test_non_numeric_dev_hours_is_rejected_not_a_500(self, app, client):
        with app.app_context():
            user = _make_user("owner@x.com", "Owner")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            db.session.add(automation)
            db.session.commit()
        _login(client, "owner@x.com")
        token = _csrf_token(client.get("/automations/thing/edit").get_data(as_text=True))
        resp = client.post("/automations/thing/edit", data={
            "csrf_token": token, "name": "Thing", "status": "live",
            "dev_hours": "багато",
        })
        assert resp.status_code == 200  # re-rendered form with an error, not a crash
        assert "введи число" in resp.get_data(as_text=True)
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.roi is None  # rejected before apply_manual_form ran

    def test_detail_page_renders_cost_card(self, app, client):
        """Real template-render smoke test, not just an ORM check - dev_hours,
        maintenance_hours_per_month, net_hours_per_month, and a linked
        subscription with its split cost must all reach automation_detail.html's
        new 'Витрати' card without a Jinja error."""
        with app.app_context():
            user = _make_user("owner@x.com", "Owner")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            sub = Subscription(name="Railway", monthly_cost_usd=20, automations=[automation])
            automation.roi = ROIEntry(measured_hours_per_month=10, maintenance_hours_per_month=4, dev_hours=40)
            db.session.add_all([automation, sub])
            db.session.commit()
        _login(client, "owner@x.com")
        resp = client.get("/automations/thing")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Витрати" in html
        assert "40" in html  # dev hours
        assert "+6.0 год/міс" in html  # net = 10 saved - 4 maintenance
        assert "Railway" in html
        assert "$20.00" in html  # sole automation on this subscription, full cost

    def test_subscriptions_checkbox_links_and_unlinks(self, app, client):
        with app.app_context():
            user = _make_user("owner@x.com", "Owner")
            automation = Automation(slug="thing", name="Thing", owner_id=user.id)
            sub = Subscription(name="Railway", monthly_cost_usd=20)
            db.session.add_all([automation, sub])
            db.session.commit()
            sub_id = sub.id
        _login(client, "owner@x.com")
        token = _csrf_token(client.get("/automations/thing/edit").get_data(as_text=True))
        client.post("/automations/thing/edit", data={
            "csrf_token": token, "name": "Thing", "status": "live",
            "subscriptions": [str(sub_id)],
        })
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert [s.name for s in automation.subscriptions] == ["Railway"]

        token = _csrf_token(client.get("/automations/thing/edit").get_data(as_text=True))
        client.post("/automations/thing/edit", data={
            "csrf_token": token, "name": "Thing", "status": "live",
        })
        with app.app_context():
            automation = Automation.query.filter_by(slug="thing").first()
            assert automation.subscriptions == []


class TestSubscriptionAdminRoutes:
    def test_non_admin_gets_403(self, app, client):
        with app.app_context():
            _make_user("a@x.com", "A", role=Role.AUTOMATOR)
        _login(client, "a@x.com")
        assert client.get("/subscriptions").status_code == 403

    def test_admin_can_create_edit_delete(self, app, client):
        with app.app_context():
            _make_user("admin@x.com", "Admin", role=Role.ADMIN)
        _login(client, "admin@x.com")

        token = _csrf_token(client.get("/subscriptions/new").get_data(as_text=True))
        resp = client.post("/subscriptions/new", data={
            "csrf_token": token, "name": "Claude Code", "provider": "Anthropic",
            "monthly_cost_usd": "200",
        })
        assert resp.status_code == 302
        with app.app_context():
            sub = Subscription.query.filter_by(name="Claude Code").first()
            assert sub is not None
            assert float(sub.monthly_cost_usd) == 200
            sub_id = sub.id

        token = _csrf_token(client.get(f"/subscriptions/{sub_id}/edit").get_data(as_text=True))
        client.post(f"/subscriptions/{sub_id}/edit", data={
            "csrf_token": token, "name": "Claude Code", "provider": "Anthropic",
            "monthly_cost_usd": "250",
        })
        with app.app_context():
            assert float(Subscription.query.get(sub_id).monthly_cost_usd) == 250

        token = _csrf_token(client.get("/subscriptions").get_data(as_text=True))
        client.post(f"/subscriptions/{sub_id}/delete", data={"csrf_token": token})
        with app.app_context():
            assert Subscription.query.get(sub_id) is None

    def test_negative_cost_rejected(self, app, client):
        with app.app_context():
            _make_user("admin@x.com", "Admin", role=Role.ADMIN)
        _login(client, "admin@x.com")
        token = _csrf_token(client.get("/subscriptions/new").get_data(as_text=True))
        resp = client.post("/subscriptions/new", data={
            "csrf_token": token, "name": "Bad", "monthly_cost_usd": "-5",
        })
        assert resp.status_code == 200
        with app.app_context():
            assert Subscription.query.filter_by(name="Bad").first() is None
