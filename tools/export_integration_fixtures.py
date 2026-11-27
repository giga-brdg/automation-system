"""Export SYNTHETIC answers of the Supplax integration API (src/integration_api.py) as contract fixtures.

    python tools/export_integration_fixtures.py <out_dir>

Builds a throw-away SQLite portfolio from the test builders (every value is marked ТЕСТ - no real data), calls every
/api/integration/v1/automation/* route through the Flask test client and writes one JSON file per contract. The
Supplax Dashboard keeps these files in scripts/fixtures/automation/ for its validator / render smoke tests; re-run
after any change of the contract."""
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "integration-fixtures")
fd, db_path = tempfile.mkstemp(suffix=".db")
os.close(fd)
for k in ("GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN", "AI_USAGE_DATABASE_URL", "RAILWAY_ENVIRONMENT"):
    os.environ.pop(k, None)
os.environ["DATABASE_URL"] = "sqlite:///" + db_path.replace("\\", "/")
os.environ["AUTH_SECRET"] = "fixtures-only"
TOKEN = "f" * 40
os.environ["INTEGRATION_API_TOKEN"] = TOKEN

from src import metrics_sync  # noqa: E402
from src.app import create_app  # noqa: E402
from src.extensions import db  # noqa: E402
from src.models import (Automation, AutomationPage, AutomationTodoItem, Department, PendingAutomation, ROIEntry,  # noqa: E402
                        ReviewLogEntry, Role, Skill, Status, Subscription, User)
from test_value_items import RUN_COST, _doc, _item, _metric  # noqa: E402
from test_at_scale import AT_SCALE  # noqa: E402

TODAY = datetime.date(2026, 10, 27)


def user(email, name, role):
    u = User(email=email, name=name, role=role, is_confirmed=True, is_approved=True)
    u.set_password("fixtures-only-pw")
    db.session.add(u)
    db.session.commit()
    return u


def sync(a, data, sha, expect_valid=True):
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    metrics_sync.apply_metrics_file(a, text, commit_sha=sha, actor=None, today=TODAY)
    db.session.commit()
    if expect_valid and metrics_sync.latest_valid_snapshot(a.id) is None:
        from src.models import MetricsSnapshot
        last = MetricsSnapshot.query.filter_by(automation_id=a.id).order_by(MetricsSnapshot.id.desc()).first()
        raise SystemExit(f"fixture metrics.json for {a.slug} is invalid: {last.errors if last else 'no snapshot'}")


app = create_app()
with app.app_context():
    db.create_all()
    admin = user("admin@fixtures.invalid", "ТЕСТ Адмін", Role.ADMIN)
    owner = user("owner@fixtures.invalid", "ТЕСТ Автоматизатор", Role.AUTOMATOR)
    sales, hr = Department(name="ТЕСТ Sales", hue=275), Department(name="ТЕСТ HR", hue=148)
    skill = Skill(name="ТЕСТ stage-1-supplax", description="ТЕСТ інтерв'ю перед стартом", repo_url="https://github.com/example/skills",
                  doc_url="https://github.com/example/skills/blob/main/SKILL.md")
    db.session.add_all([sales, hr, skill])
    db.session.commit()

    # 1) metrics format with value items (time + run_cost), at_scale, one open question, a confirmed item
    hr_bot = Automation(slug="test-hr-bot", name="ТЕСТ HR-бот", owner_id=owner.id, status=Status.LIVE,
                        one_liner="ТЕСТ відповідає кандидатам", description="ТЕСТ перший рядок\nдругий рядок\n\nТЕСТ другий абзац",
                        repo_url="https://github.com/example/hr-bot")
    hr_bot.departments.append(hr)
    hr_bot.skills.append(skill)
    db.session.add(hr_bot)
    db.session.commit()
    doc = _doc(_item(usd_per_month=932, kind="measured", basis="data", source="ТЕСТ звіт за 2026-09-20..2026-10-19"), RUN_COST,
               metrics=[_metric(measurements=[{"date": "2026-10-20", "period": "2026-09-20..2026-10-19", "value": 19,
                                               "source": "ТЕСТ звіт", "kind": "measured", "note": ""}])],
               questions=[{"metric_id": None, "value_item_id": "recruiter-hours", "field": "why", "question": "ТЕСТ чому 0,5?",
                           "ask_whom": "department_lead", "since": "2026-10-20"}])
    doc["at_scale"] = AT_SCALE
    sync(hr_bot, doc, "a" * 40)
    snap = metrics_sync.latest_valid_snapshot(hr_bot.id)
    metrics_sync.confirm_value_item(hr_bot, "recruiter-hours", snap.id, "confirmed", "ТЕСТ перевірено", admin)
    db.session.add_all([AutomationPage(automation=hr_bot, name="ТЕСТ Відповіді", description="ТЕСТ опис", detail="ТЕСТ деталі", order_index=0),
                        AutomationTodoItem(automation=hr_bot, text="ТЕСТ зробити звіт", done=False, order_index=0),
                        AutomationTodoItem(automation=hr_bot, text="ТЕСТ зроблено", done=True, order_index=1),
                        ReviewLogEntry(automation=hr_bot, round_label="ТЕСТ раунд 1", found="ТЕСТ знайдено", changed="ТЕСТ змінено", order_index=0)])

    # 2) metrics format, money on the metric, a later INVALID file (rejected badge, last accepted data kept)
    forecast = Automation(slug="test-forecast", name="ТЕСТ Прогноз", owner_id=owner.id, status=Status.LIVE)
    forecast.departments.append(sales)
    db.session.add(forecast)
    db.session.commit()
    sync(forecast, {"version": 1, "launched_at": "2026-07-22", "open_questions": [], "metrics": [{
        "id": "hours", "name": "ТЕСТ Години ручної роботи", "type": "time", "unit": "год/міс", "direction": "down", "primary": True,
        "baseline": {"value": 10, "period": "2026-06-01..2026-06-30", "source": "ТЕСТ", "kind": "measured"},
        "target": {"value": 2, "why": "ТЕСТ"}, "method": "ТЕСТ", "check_every_days": 60,
        "measurements": [{"date": "2026-09-22", "period": "2026-07-22..2026-09-21", "value": 4, "source": "ТЕСТ", "kind": "measured", "note": ""}],
        "money": {"rate_usd_per_unit": 8, "realization": 0.5, "realization_why": "ТЕСТ"}}]}, "b" * 40)
    sync(forecast, "{not json", "c" * 40, expect_valid=False)

    # 3) old format (ROIEntry only) + 4) an idea
    legacy = Automation(slug="test-legacy", name="ТЕСТ Старий формат", owner_id=owner.id, status=Status.LIVE,
                        stage1_answers={"1": {"target_metric_type": ["time_saved", "quality"]}})
    legacy.roi = ROIEntry(confidence="measured", measured_value="ТЕСТ 12 год/міс", measured_hours_per_month=12, dev_hours=20,
                          maintenance_hours_per_month=2, hypothesis="ТЕСТ гіпотеза", metric_description="ТЕСТ як міряємо")
    idea = Automation(slug="test-idea", name="ТЕСТ Ідея", owner_id=admin.id, status=Status.IDEA)
    db.session.add_all([legacy, idea])
    sub = Subscription(name="ТЕСТ Claude Code", provider="ТЕСТ Anthropic", monthly_cost_usd=100, notes="ТЕСТ")
    sub.automations.extend([hr_bot, legacy])
    db.session.add(sub)
    db.session.add(PendingAutomation(slug="test-raw", name="test-raw", repo_url="https://github.com/example/raw",
                                     missing="dashboard/SUMMARY.md (ТЕСТ)"))
    db.session.commit()

    client = app.test_client()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    out_dir.mkdir(parents=True, exist_ok=True)
    calls = {
        "value": f"value?today={TODAY.isoformat()}",
        "value-all": f"value?status=all&today={TODAY.isoformat()}",
        "registry": "automations",
        "pending": "pending",
        "detail": "automation?slug=test-legacy",
        "detail-hr-bot": "automation?slug=test-hr-bot",
        "automator": f"automator?id={owner.id}",
        "skills": "skills",
        "departments": "departments",
        "subscriptions": "subscriptions",
        "people": "people",
        "value-only": f"value?status=all&only=test-hr-bot&today={TODAY.isoformat()}",
    }
    for name, path in calls.items():
        r = client.get(f"/api/integration/v1/automation/{path}", headers=headers)
        assert r.status_code == 200, (name, r.status_code, r.get_data(as_text=True)[:300])
        body = r.get_json()
        # deterministic files: fixed envelope moments
        body["request_id"] = f"fixture-{name}"
        body["generated_at"] = "2026-10-27T09:00:00Z"
        (out_dir / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"{name}.json  {len(r.get_data())} bytes")
    db.session.remove()
    db.engine.dispose()
os.unlink(db_path)
