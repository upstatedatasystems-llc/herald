import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from herald.config import settings
from herald.db.models import TelegramPairingCode, TelegramUser

logger = logging.getLogger("herald.telegram.auth")


def get_paired_owner(db: Session) -> TelegramUser | None:
    """Return the active owner of this Herald instance, or None if unowned."""
    return (
        db.query(TelegramUser)
        .filter(TelegramUser.role == "owner", TelegramUser.is_active.is_(True))
        .first()
    )


def has_owner(db: Session) -> bool:
    """Return True if at least one active owner is paired."""
    return get_paired_owner(db) is not None


def generate_pairing_code(db: Session, expires_in_minutes: int = 15) -> str:
    """
    Generate a new random 6-digit one-time pairing code and persist it to DB.
    """
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=expires_in_minutes)

    pairing_obj = TelegramPairingCode(
        code=code,
        is_used=False,
        expires_at=exp,
        created_at=now,
    )
    db.add(pairing_obj)
    db.commit()
    db.refresh(pairing_obj)
    return code


def get_or_create_active_pairing_record(db: Session, expires_in_minutes: int = 15) -> TelegramPairingCode | None:
    """
    Return an existing valid, unexpired, unused pairing code record or generate a fresh one.
    If an owner already exists, returns None.
    """
    if has_owner(db):
        return None

    now = datetime.now(UTC)
    active = (
        db.query(TelegramPairingCode)
        .filter(
            TelegramPairingCode.is_used.is_(False),
            TelegramPairingCode.expires_at > now,
        )
        .order_by(TelegramPairingCode.created_at.desc())
        .first()
    )
    if active:
        return active

    code = "".join(secrets.choice("0123456789") for _ in range(6))
    exp = now + timedelta(minutes=expires_in_minutes)

    pairing_obj = TelegramPairingCode(
        code=code,
        is_used=False,
        expires_at=exp,
        created_at=now,
    )
    db.add(pairing_obj)
    db.commit()
    db.refresh(pairing_obj)
    return pairing_obj


def get_or_create_active_pairing_code(db: Session, expires_in_minutes: int = 15) -> str | None:
    """
    Return an existing valid, unexpired, unused pairing code string or generate a fresh one.
    If an owner already exists, returns None.
    """
    rec = get_or_create_active_pairing_record(db, expires_in_minutes=expires_in_minutes)
    return rec.code if rec else None


def verify_and_claim_pairing_code(
    db: Session,
    code: str,
    user_id: int,
    chat_id: int,
    username: str | None = None,
    first_name: str | None = None,
) -> tuple[bool, str]:
    """
    Validate a user's submitted pairing code in a private chat.
    If valid:
    - Mark code as used
    - Persist user as owner
    - Invalidate all other pending pairing codes
    """
    if has_owner(db):
        return False, "An owner is already paired for this Herald instance."

    clean_code = (code or "").strip()
    if not clean_code:
        return False, "Pairing code cannot be empty."

    now = datetime.now(UTC)
    pairing_entry = (
        db.query(TelegramPairingCode)
        .filter(
            TelegramPairingCode.code == clean_code,
            TelegramPairingCode.is_used.is_(False),
            TelegramPairingCode.expires_at > now,
        )
        .first()
    )

    if not pairing_entry:
        return False, "Invalid or expired pairing code."

    # Mark code as consumed
    pairing_entry.is_used = True
    pairing_entry.used_by_user_id = user_id
    pairing_entry.used_at = now

    # Invalidate all pending codes
    db.query(TelegramPairingCode).filter(
        TelegramPairingCode.is_used.is_(False)
    ).update({"is_used": True})

    # Save or update owner
    existing_user = (
        db.query(TelegramUser)
        .filter(TelegramUser.telegram_user_id == user_id)
        .first()
    )
    if existing_user:
        existing_user.telegram_chat_id = chat_id
        existing_user.username = username
        existing_user.first_name = first_name
        existing_user.role = "owner"
        existing_user.is_active = True
        existing_user.updated_at = now
    else:
        new_user = TelegramUser(
            telegram_user_id=user_id,
            telegram_chat_id=chat_id,
            username=username,
            first_name=first_name,
            role="owner",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(new_user)

    db.commit()
    logger.info(f"Successfully paired Telegram user '{user_id}' (Chat: '{chat_id}') as owner.")
    return True, "Pairing successful! You are now the authorized owner of this Herald instance."


def is_user_authorized(db: Session, user_id: int | str, chat_id: int | str | None = None) -> bool:
    """
    Check if a Telegram user ID and chat ID are authorized to use Herald.
    Checks DB telegram_users table as well as optional TELEGRAM_ALLOWED_USER_IDS setting.
    """
    if not user_id:
        return False

    try:
        uid_int = int(user_id)
    except (ValueError, TypeError):
        return False

    cid_int = None
    if chat_id is not None:
        try:
            cid_int = int(chat_id)
        except (ValueError, TypeError):
            pass

    # Check database authorization
    db_user = (
        db.query(TelegramUser)
        .filter(
            TelegramUser.telegram_user_id == uid_int,
            TelegramUser.is_active.is_(True),
        )
        .first()
    )
    if db_user:
        # If chat_id is provided, enforce paired private chat context
        if cid_int is not None and db_user.telegram_chat_id != cid_int:
            return False
        return True

    # Check static setting allowlist if present
    if settings.TELEGRAM_ALLOWED_USER_IDS:
        allowed_ids = [
            int(x.strip())
            for x in settings.TELEGRAM_ALLOWED_USER_IDS.split(",")
            if x.strip().isdigit()
        ]
        if uid_int in allowed_ids:
            if cid_int is not None and cid_int != uid_int:
                return False
            return True

    return False


def get_effective_user_preferences(db: Session, user_id: int | str) -> dict[str, Any]:
    """
    Retrieve user preferences from database with automatic fallback to instance settings.
    """
    try:
        uid_int = int(user_id)
    except (ValueError, TypeError):
        uid_int = None

    user = None
    if uid_int is not None:
        user = (
            db.query(TelegramUser)
            .filter(TelegramUser.telegram_user_id == uid_int, TelegramUser.is_active.is_(True))
            .first()
        )

    confirm = bool(user.confirm_before_tts) if user and user.confirm_before_tts is not None else False
    voice = (user.default_voice if user and user.default_voice else None) or getattr(settings, "KOKORO_VOICE", "af_heart")
    speed = (user.default_speed if user and user.default_speed else None) or getattr(settings, "KOKORO_SPEED", 1.0)
    mode = (user.default_mode if user and user.default_mode else None) or settings.get_default_mode()

    return {
        "confirm_before_tts": confirm,
        "default_voice": voice,
        "default_speed": float(speed),
        "default_mode": mode,
        "ai_provider": getattr(settings, "AI_PROVIDER", None) or "None (Literal only)",
    }


def set_user_confirm_before_tts(db: Session, user_id: int | str, enabled: bool) -> bool:
    """Set confirm_before_tts preference for a Telegram user."""
    try:
        uid_int = int(user_id)
    except (ValueError, TypeError):
        return False

    user = (
        db.query(TelegramUser)
        .filter(TelegramUser.telegram_user_id == uid_int, TelegramUser.is_active.is_(True))
        .first()
    )
    if not user:
        return False

    user.confirm_before_tts = bool(enabled)
    user.updated_at = datetime.now(UTC)
    db.commit()
    logger.info(f"Updated confirm_before_tts={enabled} for Telegram user '{uid_int}'.")
    return True


def set_user_default_voice(db: Session, user_id: int | str, voice: str | None) -> bool:
    """Set default_voice preference for a Telegram user."""
    try:
        uid_int = int(user_id)
    except (ValueError, TypeError):
        return False

    user = (
        db.query(TelegramUser)
        .filter(TelegramUser.telegram_user_id == uid_int, TelegramUser.is_active.is_(True))
        .first()
    )
    if not user:
        return False

    if voice is not None:
        allowed = settings.get_allowed_voices_list()
        if voice.lower() not in allowed:
            raise ValueError(f"Voice '{voice}' is not in allowed voices: {allowed}")
        user.default_voice = voice.lower()
    else:
        user.default_voice = None

    user.updated_at = datetime.now(UTC)
    db.commit()
    return True


def set_user_default_speed(db: Session, user_id: int | str, speed: float | None) -> bool:
    """Set default_speed preference for a Telegram user."""
    try:
        uid_int = int(user_id)
    except (ValueError, TypeError):
        return False

    user = (
        db.query(TelegramUser)
        .filter(TelegramUser.telegram_user_id == uid_int, TelegramUser.is_active.is_(True))
        .first()
    )
    if not user:
        return False

    if speed is not None:
        s_float = float(speed)
        if not (settings.MIN_SPEED <= s_float <= settings.MAX_SPEED):
            raise ValueError(f"Speed {s_float} out of range ({settings.MIN_SPEED} to {settings.MAX_SPEED})")
        user.default_speed = s_float
    else:
        user.default_speed = None

    user.updated_at = datetime.now(UTC)
    db.commit()
    return True


def set_user_default_mode(db: Session, user_id: int | str, mode: str | None) -> bool:
    """Set default_mode preference for a Telegram user."""
    try:
        uid_int = int(user_id)
    except (ValueError, TypeError):
        return False

    user = (
        db.query(TelegramUser)
        .filter(TelegramUser.telegram_user_id == uid_int, TelegramUser.is_active.is_(True))
        .first()
    )
    if not user:
        return False

    if mode is not None:
        m_clean = mode.lower().strip()
        if m_clean not in ("literal", "brief", "standard", "research"):
            raise ValueError(f"Invalid mode '{mode}'")
        user.default_mode = m_clean
    else:
        user.default_mode = None

    user.updated_at = datetime.now(UTC)
    db.commit()
    return True
