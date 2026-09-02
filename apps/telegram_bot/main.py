import logging
import sys
from herald.ai.factory import get_ai_provider
from herald.config import settings
from herald.db.connection import SessionLocal
from herald.telegram.auth import get_or_create_active_pairing_code, has_owner
from herald.telegram.bot import run_telegram_bot_loop
from herald.telegram.client import TelegramClient
from herald.tts.kokoro_client import KokoroClient

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
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

    ai_provider = get_ai_provider()
    ai_status = "Not configured (Literal mode only)"
    if ai_provider and settings.is_ai_configured():
        try:
            conn_res = ai_provider.check_connection(timeout_seconds=4.0)
            if conn_res.get("connected"):
                ai_status = f"{ai_provider.provider_name} — Connected ({conn_res.get('model')})"
            else:
                ai_status = f"{ai_provider.provider_name} — FAILED ({conn_res.get('error')})"
        except Exception as e:
            ai_status = f"{ai_provider.provider_name} — Error ({e})"

    print("\n" + "=" * 60)
    print("                HERALD PODCAST BOT")
    print("=" * 60)
    print(f"Telegram:   {'Connected (' + bot_name + ')' if bot_connected else 'Disconnected / Token Invalid'}")
    print(f"TTS:        {'Ready (Kokoro)' if tts_ok else 'Unavailable / Offline'}")
    print(f"AI:         {ai_status}")

    db = SessionLocal()
    try:
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
    finally:
        db.close()
    print("=" * 60 + "\n")


def main():
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set in environment or .env. Exiting.")
        sys.exit(1)

    print_startup_banner()
    run_telegram_bot_loop()


if __name__ == "__main__":
    main()
