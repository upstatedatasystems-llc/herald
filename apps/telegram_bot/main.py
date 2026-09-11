import logging
import sys

from herald.config import settings
from herald.db.connection import SessionLocal
from herald.logging import setup_service_logging
from herald.telegram.auth import get_or_create_active_pairing_code, has_owner
from herald.telegram.bot import run_telegram_bot
from herald.telegram.client import TelegramClient
from herald.tts.kokoro_client import KokoroClient

setup_service_logging("telegram-bot")
logger = logging.getLogger("herald.telegram.daemon")


def print_startup_banner():
    client = TelegramClient()
    bot_name = "Unknown"
    bot_connected = False
    if client.is_configured:
        try:
            bot_info = client.get_me()
            bot_name = f"@{bot_info.get('username', 'HeraldBot')}"
            bot_connected = True
        except Exception as e:
            bot_name = f"Error: {e}"

    tts_ok = False
    try:
        k_res = KokoroClient().health_check()
        tts_ok = bool(isinstance(k_res, dict) and k_res.get("healthy"))
    except Exception:
        tts_ok = False

    from herald.ai.registry import (
        create_provider,
        get_default_model,
        get_descriptor,
        is_provider_configured,
    )
    from herald.ai.resolution import get_server_default_chain

    ai_status = "Not configured (Literal mode only)"
    try:
        def_chain = get_server_default_chain()
        primary_id = def_chain[0] if def_chain else "literal"
        if primary_id == "literal":
            ai_status = "Literal (Zero-AI narration)"
        elif not is_provider_configured(primary_id):
            desc = get_descriptor(primary_id)
            p_name = desc.display_name if desc else primary_id.capitalize()
            ai_status = f"{p_name} — Not configured on server"
        else:
            default_model = get_default_model(primary_id)
            ai_provider = create_provider(primary_id, model_id=default_model)
            conn_res = ai_provider.check_connection(timeout_seconds=4.0)
            if conn_res.get("connected"):
                ai_status = f"{ai_provider.provider_name} — Connected ({conn_res.get('model', default_model)})"
            else:
                ai_status = f"{ai_provider.provider_name} — FAILED ({conn_res.get('error')})"
    except Exception as e:
        ai_status = f"Chain error ({e})"

    print("\n" + "=" * 60)
    print("                HERALD PODCAST BOT")
    print("=" * 60)
    print(f"Telegram:   {'Connected (' + bot_name + ')' if bot_connected else 'Disconnected / Token Invalid'}")
    print(f"TTS:        {'Ready (Kokoro)' if tts_ok else 'Unavailable / Offline'}")
    print(f"AI:         {ai_status}")

    with SessionLocal() as db:
        if not has_owner(db):
            code = get_or_create_active_pairing_code(db, expires_in_minutes=30)
            print("\n" + "-" * 60)
            print("  NO OWNER PAIRED YET!")
            print(f"  To pair your account, message your bot ({bot_name}):")
            print(f"\n      /pair {code}\n")
            print("  After pairing, use /help for instructions.")
            print("-" * 60 + "\n")
        else:
            print("\nOwner:      Paired")
            print("Status:     Ready to accept jobs\n")
    print("=" * 60 + "\n")


TELEGRAM_BOT_COMMANDS = [
    {"command": "start", "description": "Start bot and view quick-start guide"},
    {"command": "help", "description": "View usage guide, modes, and directives"},
    {"command": "download", "description": "Download podcast audio MP3"},
    {"command": "diagnostics", "description": "View job diagnostics and support export"},
    {"command": "status", "description": "View system health, queue depth, and uptime"},
    {"command": "ai_check", "description": "Test AI provider connection"},
    {"command": "queue", "description": "View active podcast queue"},
    {"command": "settings", "description": "Configure preferences and default voice"},
    {"command": "readme", "description": "Download project documentation"},
]
TELEGRAM_BOT_COMMANDS_2A = [c for c in TELEGRAM_BOT_COMMANDS if c["command"] != "settings"]


def register_bot_commands(client: TelegramClient) -> bool:
    """Register supported Telegram bot commands with setMyCommands."""
    try:
        ok = client.set_my_commands(TELEGRAM_BOT_COMMANDS)
        if ok:
            logger.info("Successfully registered Telegram bot commands with setMyCommands.")
        else:
            logger.warning("Telegram setMyCommands returned non-ok result.")
        return ok
    except Exception as e:
        logger.warning(f"Non-fatal error registering Telegram commands: {e}")
        return False


def main():
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set in environment or .env. Exiting.")
        sys.exit(1)

    # Validate server default AI provider chain at startup boundary
    from herald.ai.registry import validate_server_default_chain
    is_valid, err = validate_server_default_chain(
        settings.AI_PROVIDER,
        settings.AI_SECONDARY_PROVIDER,
        settings.AI_TERTIARY_PROVIDER,
    )
    if not is_valid:
        logger.error("Invalid server default AI provider chain: %s", err)
        if settings.HERALD_ENV.lower() == "production":
            raise RuntimeError(f"Invalid server default AI provider chain: {err}")

    client = TelegramClient()
    print_startup_banner()
    register_bot_commands(client)
    run_telegram_bot()


if __name__ == "__main__":
    main()
