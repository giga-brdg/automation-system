"""Covers src/integration_api.py - the read-only JSON the Supplax Dashboard reads (contracts automation.*).

Synthetic test data only (the same builders as test_value_metrics.py). What must hold:
  * off by default (no INTEGRATION_API_TOKEN -> 503), bearer token required (401), GET only;
  * the numbers are exactly value_dashboard.build_context's (no second implementation of any formula) and the
    *_text fields are the HTML page's own formatting;
  * never an e-mail, a password hash, an api_key or a registration code;
  * read-only: nothing in the DB changes, however often it is called."""
import datetime
import json

import pytest

from test_value_metrics import TODAY, _automation, _confirm, _sync, _time_doc, _user

from src import value_dashboard as vd
from src.extensions import db
from src.models import (Automation, AutomationEvent, Department, MetricsSnapshot, PendingAutomation, ROIEntry, Role, Skill,
                        Status, Subscription)

TOKEN = "t" * 40
BASE = "/api/integration/v1/automation"
ENDPOINTS = ["value", "value?status=all", "automations", "pending", "automation?slug=hr-bot", "automator?id=1", "skills",
             "departments", "subscriptions"]


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("INTEGRATION_API_TOKEN", TOKEN)
    return TOKEN


def _get(client, path, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(f"{BASE}/{path}", headers=headers)


@pytest.fixture
def portfolio(app):
    """Two live automations (one confirmed money, one old-format with ROI hours) + an idea; a department, a skill,
    a subscription and a pending repo."""
    admin = _user("admin@example.com", "Адмін Тест")
    owner = _user("owner@example.com", "Автоматизатор Тест", role=Role.AUTOMATOR)
    sales = Department(name="Sales", hue=275)
    skill = Skill(name="stage-1-supplax", description="Інтерв'ю", repo_url="https://github.com/x/y")
    db.session.add_all([sales, skill])
    db.session.commit()
    hr_bot = _automation(owner, "hr-bot", one_liner="Бот для HR", description="рядок один\nрядок два\n\nновий абзац")
    hr_bot.departments.append(sales)
    hr_bot.skills.append(skill)
    _sync(hr_bot, _time_doc(after=4))
    _confirm(hr_bot, admin, "confirmed")
    legacy = _automation(owner, "legacy", repo_url="https://github.com/x/legacy")
    legacy.roi = ROIEntry(confidence="measured", measured_value="12 год/міс", measured_hours_per_month=12,
                          dev_hours=20, maintenance_hours_per_month=2)
    _automation(owner, "an-idea", status=Status.IDEA)
    sub = Subscription(name="Claude Code — Team", provider="Anthropic", monthly_cost_usd=100)
    sub.automations.extend([hr_bot, legacy])
    db.session.add(sub)
    db.session.add(PendingAutomation(slug="raw-repo", name="raw-repo", repo_url="https://github.com/x/raw", missing="dashboard/SUMMARY.md"))
    db.session.commit()
    return {"admin": admin, "owner": owner, "hr_bot": hr_bot, "legacy": legacy, "department": sales}


class TestAuth:
    def test_disabled_without_token_env(self, client, portfolio, monkeypatch):
        monkeypatch.delenv("INTEGRATION_API_TOKEN", raising=False)
        r = _get(client, "value")
        assert r.status_code == 503 and r.get_json()["error"]["code"] == "integration_disabled"

    def test_short_token_env_counts_as_disabled(self, client, portfolio, monkeypatch):
        monkeypatch.setenv("INTEGRATION_API_TOKEN", "short")
        assert _get(client, "value", token="short").status_code == 503

    @pytest.mark.parametrize("given", [None, "wrong" * 10, TOKEN + "x"])
    def test_wrong_or_missing_token(self, client, portfolio, token, given):
        assert _get(client, "value", token=given).status_code == 401

    def test_user_api_key_is_not_a_service_token(self, client, portfolio, token):
        r = client.get(f"{BASE}/value", headers={"X-API-Key": portfolio["admin"].api_key})
        assert r.status_code == 401

    def test_get_only(self, client, portfolio, token):
        r = client.post(f"{BASE}/value", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 405


class TestEnvelope:
    @pytest.mark.parametrize("path, contract", [("value", "automation.value"), ("automations", "automation.registry"),
                                                ("pending", "automation.pending"), ("automation?slug=hr-bot", "automation.detail"),
                                                ("automator?id=1", "automation.automator"), ("skills", "automation.skills"),
                                                ("departments", "automation.departments"), ("subscriptions", "automation.subscriptions")])
    def test_common_envelope(self, client, portfolio, token, path, contract):
        r = _get(client, path)
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert body["api_version"] == "1.0" and body["contract"] == contract and body["period"] is None
        assert body["freshness"]["status"] in ("fresh", "stale", "no_data") and body["freshness"]["stale_after_hours"] == 36
        assert body["generated_at"].endswith("Z") and len(body["request_id"]) <= 64
        assert r.headers["Cache-Control"] == "no-store"

    def test_request_id_is_echoed_when_safe(self, client, portfolio, token):
        r = client.get(f"{BASE}/skills", headers={"Authorization": f"Bearer {TOKEN}", "X-Request-Id": "abc-123"})
        assert r.get_json()["request_id"] == "abc-123"
        r = client.get(f"{BASE}/skills", headers={"Authorization": f"Bearer {TOKEN}", "X-Request-Id": "<script>"})
        assert r.get_json()["request_id"] != "<script>"


class TestValueParity:
    def test_totals_are_build_context(self, client, portfolio, token):
        body = _get(client, f"value?today={TODAY.isoformat()}").get_json()
        live = Automation.query.filter(Automation.status == Status.LIVE).order_by(Automation.name).all()
        ctx = vd.build_context(live, today=TODAY)
        t = body["totals"]
        for key in ("total_n", "saved_total", "saved_measured", "saved_estimated", "partial_n", "rejected_verdict_n", "old_format_n",
                    "confidence_pct", "measured_auto_n", "has_money", "money_total", "at_scale_total", "at_scale_n", "dev_total",
                    "maint_total", "subs_cost_total", "subs_linked_count", "net_scale"):
            assert t[key] == (float(ctx[key]) if isinstance(ctx[key], float) else ctx[key]), key
        assert t["tiles"] == ctx["tiles"] and t["money"] == ctx["money"] and t["money_seen"] == ctx["money_seen"]
        assert [r["automation"]["slug"] for r in body["rows"]] == [r["automation"].slug for r in ctx["rows"]]

    def test_money_and_texts(self, client, portfolio, token):
        body = _get(client, f"value?today={TODAY.isoformat()}").get_json()
        # 10 -> 4 год/міс, 8 $/год x 0.5 = $24, confirmed by the admin
        assert body["totals"]["money"]["confirmed"] == 24.0 and body["totals"]["money_total"] == 24.0
        assert body["texts"]["money_total"] == vd.fmt_usd(24.0) == "$24"
        assert body["texts"]["subs_cost_total"] == vd.fmt_usd(100)
        hr = next(r for r in body["rows"] if r["automation"]["slug"] == "hr-bot")
        primary = next(m for m in hr["metrics"] if m["key"] == hr["primary_key"])
        assert primary["status"] == "confirmed" and primary["verdict"] == "confirmed" and primary["money_measured"] == 24.0
        assert primary["baseline_text"] == "10" and primary["latest_measured_text"] == "4" and primary["trend"] == "better"
        assert hr["money_summary"]["headline"] == "$24/міс, підтверджено" and hr["tile"] == "confirmed"

    def test_old_format_row_and_net_balance(self, client, portfolio, token):
        body = _get(client, f"value?today={TODAY.isoformat()}").get_json()
        legacy = next(r for r in body["rows"] if r["automation"]["slug"] == "legacy")
        assert legacy["format"] == "old" and legacy["metrics"] == [] and legacy["tile"] == "measured"
        assert legacy["roi"]["best_hours_saved_per_month"] == 12.0 and legacy["roi"]["best_hours_text"] == "12,0"
        net = {r["slug"]: r for r in body["net_rows"]}
        assert net["legacy"]["net"] == 10.0 and net["legacy"]["net_text"] == "+10,0"

    def test_status_filter_and_options(self, client, portfolio, token):
        live = _get(client, "value").get_json()
        every = _get(client, "value?status=all").get_json()
        assert live["filters"]["status"] == "live" and live["totals"]["total_n"] == 2
        assert every["filters"]["status"] == "all" and every["totals"]["total_n"] == 3
        assert [o["value"] for o in live["options"]["statuses"]][-1] == "all"
        assert live["options"]["departments"][0]["name"] == "Sales"
        dept = portfolio["department"].id
        assert _get(client, f"value?department={dept}").get_json()["totals"]["total_n"] == 1

    def test_empty_portfolio_keeps_nulls(self, client, app, token):
        body = _get(client, "value").get_json()
        t = body["totals"]
        assert t["total_n"] == 0 and t["saved_total"] is None and t["money_total"] is None and t["confidence_pct"] is None
        assert body["texts"]["money_total"] == "—" and body["rows"] == [] and body["freshness"]["status"] == "no_data"


class TestOtherResources:
    def test_registry(self, client, portfolio, token):
        body = _get(client, "automations").get_json()
        assert body["counts"]["total"] == 3 and body["counts"]["by_status"]["live"] == 2 and body["counts"]["needs_review"] == 3
        assert {r["slug"] for r in body["rows"]} == {"hr-bot", "legacy", "an-idea"}
        legacy = next(r for r in body["rows"] if r["slug"] == "legacy")
        assert legacy["roi_text"] == "12 год/міс" and legacy["security"]["state"] == "none"
        assert _get(client, "automations?status=idea").get_json()["rows"][0]["slug"] == "an-idea"
        assert _get(client, "automations?q=legacy").get_json()["rows"][0]["slug"] == "legacy"

    def test_detail(self, client, portfolio, token):
        body = _get(client, "automation?slug=legacy").get_json()
        a = body["automation"]
        assert a["slug"] == "legacy" and a["repo_url"] == "https://github.com/x/legacy" and body["tokens"] is None
        assert body["costs"]["dev_hours"] == 20.0 and body["costs"]["net_hours_per_month"] == 10.0
        assert body["costs"]["subscriptions"] == [{"name": "Claude Code — Team", "cost_per_automation": 50.0, "shared_by": 2}]
        hr = _get(client, "automation?slug=hr-bot").get_json()
        assert hr["automation"]["description"] == "рядок один рядок два\n\nновий абзац"  # the page's own reflow
        assert hr["skills"] == [{"id": 1, "name": "stage-1-supplax"}]

    def test_detail_errors(self, client, portfolio, token):
        assert _get(client, "automation?slug=nope").status_code == 404
        assert _get(client, "automation").status_code == 400

    def test_people_skills_departments_subscriptions(self, client, portfolio, token):
        person = _get(client, f"automator?id={portfolio['owner'].id}").get_json()
        assert person["person"]["name"] == "Автоматизатор Тест" and person["person"]["role"] == "automator"
        assert [a["slug"] for a in person["automations"]] == ["an-idea", "hr-bot", "legacy"]
        assert _get(client, "automator?id=999").status_code == 404
        skills = _get(client, "skills").get_json()["rows"]
        assert skills[0]["automations"] == [{"slug": "hr-bot", "name": "hr-bot"}]
        assert _get(client, "departments").get_json()["rows"] == [{"id": portfolio["department"].id, "name": "Sales", "hue": 275, "automations_n": 1}]
        subs = _get(client, "subscriptions").get_json()
        assert subs["total_monthly_usd"] == 100.0 and subs["rows"][0]["automations_n"] == 2
        assert _get(client, "pending").get_json()["rows"][0]["slug"] == "raw-repo"

    @pytest.mark.parametrize("path", ["value?status=bogus", "automations?status=all", "value?department=x", "value?today=2026-13-01", "automator?id=0"])
    def test_bad_params_are_400(self, client, portfolio, token, path):
        assert _get(client, path).status_code == 400


class TestSafety:
    def test_no_secrets_or_emails_anywhere(self, client, portfolio, token):
        admin, owner = portfolio["admin"], portfolio["owner"]
        for path in ENDPOINTS:
            text = _get(client, path).get_data(as_text=True)
            for secret in (admin.api_key, owner.api_key, admin.password_hash, owner.password_hash, "admin@example.com", "owner@example.com"):
                assert secret not in text, path
            for key in ('"api_key"', '"password_hash"', '"email"', '"pending_code"', '"github_username"'):
                assert key not in text, path

    def test_read_only(self, client, portfolio, token):
        def counts():
            return (Automation.query.count(), MetricsSnapshot.query.count(), AutomationEvent.query.count(),
                    PendingAutomation.query.count(), [a.updated_at for a in Automation.query.order_by(Automation.id)])
        before = counts()
        for _ in range(2):
            for path in ENDPOINTS:
                assert _get(client, path).status_code == 200, path
        db.session.expire_all()
        assert counts() == before

    def test_html_pages_unchanged_by_the_blueprint(self, client, portfolio, token):
        # The session routes still require a login (the token opens only the JSON API)
        r = client.get("/value", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 302 and "/login" in r.headers["Location"]


class TestViewerScope:
    """`only` (the Supplax viewer's allowed set) and /people (the e-mail match) — 2026-11-27."""

    def test_only_restricts_value_and_recomputes_totals(self, client, portfolio, token):
        body = _get(client, f"value?status=all&only=hr-bot&today={TODAY.isoformat()}").get_json()
        ctx = vd.build_context([portfolio["hr_bot"]], today=TODAY)
        assert [r["automation"]["slug"] for r in body["rows"]] == ["hr-bot"]
        assert body["totals"]["total_n"] == ctx["total_n"] == 1 and body["totals"]["money"] == ctx["money"]
        assert [o["name"] for o in body["options"]["owners"]] == ["Автоматизатор Тест"]
        assert [s["name"] for s in body["subscriptions"]] == ["Claude Code — Team"]

    def test_only_empty_means_nothing(self, client, portfolio, token):
        body = _get(client, "value?status=all&only=").get_json()
        assert body["rows"] == [] and body["totals"]["total_n"] == 0 and body["subscriptions"] == [] and body["options"]["owners"] == []
        reg = _get(client, "automations?only=").get_json()
        assert reg["rows"] == [] and reg["counts"]["total"] == 0 and reg["departments"] == []

    def test_only_restricts_registry_counts(self, client, portfolio, token):
        reg = _get(client, "automations?only=legacy,an-idea").get_json()
        assert sorted(r["slug"] for r in reg["rows"]) == ["an-idea", "legacy"]
        assert reg["counts"]["total"] == 2 and reg["counts"]["by_status"]["idea"] == 1

    @pytest.mark.parametrize("bad", ["a b", "x" * 121, ",".join(f"s{i}" for i in range(1001)), "../etc"])
    def test_only_is_validated(self, client, portfolio, token, bad):
        from urllib.parse import quote
        assert _get(client, f"value?only={quote(bad)}").status_code == 400

    def test_people_has_work_email_and_ownership_only(self, client, portfolio, token):
        r = _get(client, "people")
        assert r.status_code == 200 and r.get_json()["contract"] == "automation.people"
        rows = {p["email"]: p for p in r.get_json()["rows"]}
        assert set(rows) == {"admin@example.com", "owner@example.com"}
        assert rows["owner@example.com"]["automations"] == ["an-idea", "hr-bot", "legacy"] and rows["owner@example.com"]["role"] == "automator"
        text = r.get_data(as_text=True)
        for secret in (portfolio["admin"].api_key, portfolio["owner"].password_hash):
            assert secret not in text
        for key in ('"api_key"', '"password_hash"', '"pending_code"', '"github_username"'):
            assert key not in text
