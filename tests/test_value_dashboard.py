"""Covers src/value_dashboard.py's aggregation (build_context, target_metrics)
directly, plus the /value route (src/app.py's value_dashboard_page) - renders,
department/owner filters, and that an automation with no Stage 1 answers at
all (pre-dates this feature, or just hasn't run it yet) doesn't error."""
import re

from src.extensions import db
from src.models import Automation, Department, ROIEntry, Role, Subscription, User
from src.value_dashboard import build_context, target_metrics


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


class TestTargetMetrics:
    def test_empty_when_no_stage1_answers(self, app):
        with app.app_context():
            a = Automation(slug="a", name="A", owner_id=1)
            assert target_metrics(a) == []

    def test_reads_phase1_target_metric_type(self, app):
        with app.app_context():
            a = Automation(slug="a", name="A", owner_id=1,
                            stage1_answers={"1": {"target_metric_type": ["time_saved", "quality"]}})
            assert target_metrics(a) == ["time_saved", "quality"]

    def test_ignores_unknown_tokens(self, app):
        with app.app_context():
            a = Automation(slug="a", name="A", owner_id=1,
                            stage1_answers={"1": {"target_metric_type": ["time_saved", "bogus"]}})
            assert target_metrics(a) == ["time_saved"]


class TestBuildContext:
    def test_empty_portfolio(self, app):
        with app.app_context():
            ctx = build_context([])
            assert ctx["saved_total"] is None
            assert ctx["total_n"] == 0
            assert ctx["confidence_pct"] is None
            assert ctx["net_rows"] == []
            assert ctx["dev_total"] is None
            assert ctx["subs_cost_total"] is None

    def test_saved_hours_split_measured_vs_estimated(self, app):
        with app.app_context():
            owner = _make_user("o@x.com", "O")
            measured = Automation(slug="m", name="M", owner_id=owner.id,
                                   roi=ROIEntry(measured_hours_per_month=5))
            estimated = Automation(slug="e", name="E", owner_id=owner.id,
                                    roi=ROIEntry(baseline_cycle_minutes=60, baseline_frequency_per_month=4,
                                                 target_cycle_minutes=0, target_frequency_per_month=4))
            db.session.add_all([measured, estimated])
            db.session.commit()
            ctx = build_context([measured, estimated])
            assert ctx["saved_total"] == 9  # 5 measured + 4 estimated
            assert ctx["saved_measured"] == 5
            assert ctx["saved_estimated"] == 4

    def test_confidence_pct_only_counts_automations_with_roi(self, app):
        with app.app_context():
            owner = _make_user("o@x.com", "O")
            a1 = Automation(slug="a1", name="A1", owner_id=owner.id, roi=ROIEntry(confidence="measured"))
            a2 = Automation(slug="a2", name="A2", owner_id=owner.id, roi=ROIEntry(confidence="estimated"))
            a3 = Automation(slug="a3", name="A3", owner_id=owner.id)  # no roi at all
            db.session.add_all([a1, a2, a3])
            db.session.commit()
            ctx = build_context([a1, a2, a3])
            assert ctx["measured_n"] == 1
            assert ctx["estimated_n"] == 1
            assert ctx["none_n"] == 1
            assert ctx["confidence_pct"] == 50  # 1 of 2 *with* an roi entry, not 1 of 3

    def test_dev_hours_excluded_from_net_hours(self, app):
        with app.app_context():
            owner = _make_user("o@x.com", "O")
            a = Automation(slug="a", name="A", owner_id=owner.id,
                            roi=ROIEntry(measured_hours_per_month=10, maintenance_hours_per_month=4, dev_hours=999))
            db.session.add(a)
            db.session.commit()
            ctx = build_context([a])
            assert ctx["net_rows"] == [{"automation": a, "net": 6.0, "measured": True}]
            assert ctx["dev_total"] == 999  # dev_hours still totalled on its own, just not blended into net

    def test_metric_counts_from_stage1_answers(self, app):
        with app.app_context():
            owner = _make_user("o@x.com", "O")
            a1 = Automation(slug="a1", name="A1", owner_id=owner.id,
                             stage1_answers={"1": {"target_metric_type": ["time_saved", "cost"]}})
            a2 = Automation(slug="a2", name="A2", owner_id=owner.id,
                             stage1_answers={"1": {"target_metric_type": ["time_saved"]}})
            db.session.add_all([a1, a2])
            db.session.commit()
            ctx = build_context([a1, a2])
            counts = {m["label"]: m["count"] for m in ctx["metric_counts"]}
            assert counts["Час ручної праці"] == 2
            assert counts["Витрати"] == 1
            assert counts["Якість"] == 0

    def test_subscription_cost_split_across_automations(self, app):
        with app.app_context():
            owner = _make_user("o@x.com", "O")
            a1 = Automation(slug="a1", name="A1", owner_id=owner.id)
            a2 = Automation(slug="a2", name="A2", owner_id=owner.id)
            sub = Subscription(name="VPS", monthly_cost_usd=100, automations=[a1, a2])
            db.session.add_all([a1, a2, sub])
            db.session.commit()
            ctx = build_context([a1, a2])
            assert ctx["subs_cost_total"] == 100  # 50 + 50
            assert ctx["subs_linked_count"] == 1


class TestValueDashboardRoute:
    def test_renders_with_no_automations(self, app, client):
        with app.app_context():
            _make_user("a@x.com", "A")
        _login(client, "a@x.com")
        resp = client.get("/value")
        assert resp.status_code == 200
        assert "Цінність автоматизацій".encode() in resp.data

    def test_renders_automation_with_no_stage1_and_no_roi(self, app, client):
        """Pre-dates the feature entirely - must render as an honest gap, not error."""
        with app.app_context():
            user = _make_user("a@x.com", "A")
            db.session.add(Automation(slug="thing", name="Thing", owner_id=user.id))
            db.session.commit()
        _login(client, "a@x.com")
        resp = client.get("/value")
        assert resp.status_code == 200
        assert "Невідомо".encode() in resp.data
        assert "Немає ROI".encode() in resp.data

    def test_department_filter(self, app, client):
        with app.app_context():
            user = _make_user("a@x.com", "A")
            sales = Department(name="Sales", hue=10)
            hr = Department(name="HR", hue=20)
            a1 = Automation(slug="a1", name="A1", owner_id=user.id, departments=[sales])
            a2 = Automation(slug="a2", name="A2", owner_id=user.id, departments=[hr])
            db.session.add_all([sales, hr, a1, a2])
            db.session.commit()
            sales_id = sales.id
        _login(client, "a@x.com")
        resp = client.get(f"/value?department={sales_id}")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "A1" in html
        assert "A2" not in html

    def test_owner_filter(self, app, client):
        with app.app_context():
            u1 = _make_user("u1@x.com", "Owner One")
            u2 = _make_user("u2@x.com", "Owner Two")
            db.session.add(Automation(slug="a1", name="A1", owner_id=u1.id))
            db.session.add(Automation(slug="a2", name="A2", owner_id=u2.id))
            db.session.commit()
            u1_id = u1.id
        _login(client, "u1@x.com")
        resp = client.get(f"/value?owner={u1_id}")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "A1" in html
        assert "A2" not in html
