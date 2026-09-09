"""Registration-approval Telegram bot. Run as its own process:

    python -m src.telegram_bot

Polls Telegram for messages (no public URL needed, unlike a webhook) and
only ever acts on messages from ADMIN_TELEGRAM_CHAT_ID - every other chat is
ignored outright, so this is safe to leave running even though the bot
token itself has no separate access control. See /register and /confirm in
app.py for the other half of the flow.

Commands (admin only):
    /pending                    - list accounts awaiting confirmation/approval
    /grant <email> <role>       - role is 'admin', 'automator', or 'viewer';
                                   requires the account to have already
                                   confirmed its code
    /revoke <email>             - remove access without deleting the account
    /help                       - list commands
"""
import os
import secrets
import time

from . import telegram
from .app import create_app
from .extensions import db
from .models import Role, User

POLL_TIMEOUT = 25  # seconds - Telegram's own long-poll window


def _admin_chat_id():
    raw = os.environ.get("ADMIN_TELEGRAM_CHAT_ID")
    return int(raw) if raw else None


def handle_command(text, reply):
    parts = text.strip().split()
    cmd = parts[0].lower()

    if cmd == "/pending":
        pending = User.query.filter_by(is_approved=False).order_by(User.created_at).all()
        if not pending:
            reply("Немає користувачів, що очікують доступу.")
            return
        lines = [
            f"- {u.name} <{u.email}> — {'підтвердив код' if u.is_confirmed else 'ще не ввів код'}"
            for u in pending
        ]
        reply("\n".join(lines))
    elif cmd == "/grant" and len(parts) >= 3:
        email, role_raw = parts[1].strip().lower(), parts[2].strip().lower()
        roles_by_name = {"admin": Role.ADMIN, "automator": Role.AUTOMATOR, "viewer": Role.VIEWER}
        if role_raw not in roles_by_name:
            reply("Роль має бути 'admin', 'automator' або 'viewer'.")
            return
        user = User.query.filter_by(email=email).first()
        if not user:
            reply(f"Не знайдено користувача {email}.")
            return
        if not user.is_confirmed:
            reply(f"{email} ще не ввів код підтвердження — зачекай.")
            return
        user.role = roles_by_name[role_raw]
        user.is_approved = True
        # Rotate rather than trust whatever api_key the row already has -
        # every User gets one at INSERT time (a column default, see
        # models.py), self-registration included, so without this a
        # self-registered account's key has been sitting live since signup,
        # never freshly issued at the moment access actually starts. Report
        # it here only for Automator/Admin - a Viewer's key is inert anyway
        # (api_sync_automation requires that role), no need to surface it.
        user.api_key = secrets.token_hex(32)
        db.session.commit()
        if role_raw in ("automator", "admin"):
            reply(f"Готово: {email} тепер {role_raw}.\nAPI-ключ (для API-синку автоматизацій): {user.api_key}")
        else:
            reply(f"Готово: {email} тепер {role_raw}.")
    elif cmd == "/revoke" and len(parts) >= 2:
        email = parts[1].strip().lower()
        user = User.query.filter_by(email=email).first()
        if not user:
            reply(f"Не знайдено користувача {email}.")
            return
        user.is_approved = False
        # Rotate api_key too, not just the approval flag - api_sync_automation
        # now also checks is_approved (see src/app.py), so this is
        # defense-in-depth rather than the only thing stopping it, but a
        # revoked account's old key still shouldn't keep validating against
        # anything that might check role alone.
        user.api_key = secrets.token_hex(32)
        db.session.commit()
        reply(f"Доступ {email} відкликано.")
    elif cmd == "/help":
        reply(
            "/pending — список тих, хто чекає доступу\n"
            "/grant <email> <admin|automator|viewer> — надати доступ\n"
            "/revoke <email> — забрати доступ"
        )
    else:
        reply("Не розпізнав команду. /help — список команд.")


def run():
    admin_chat_id = _admin_chat_id()
    if not admin_chat_id:
        raise SystemExit("ADMIN_TELEGRAM_CHAT_ID не задано в .env")

    app = create_app()
    offset = None
    print("Telegram bot polling started.")
    while True:
        try:
            updates = telegram.get_updates(offset=offset, timeout=POLL_TIMEOUT)
        except Exception as e:
            print(f"getUpdates failed: {e}")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat_id = (message.get("chat") or {}).get("id")
            text = message.get("text") or ""
            if chat_id != admin_chat_id or not text.startswith("/"):
                continue  # not the admin, or not a command - ignore silently
            with app.app_context():
                handle_command(text, lambda t: telegram.send_message(admin_chat_id, t))


if __name__ == "__main__":
    run()
