import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import Base, TelegramUser
from herald.telegram.auth import (
    generate_pairing_code,
    get_effective_user_preferences,
    has_owner,
    set_user_confirm_before_tts,
    set_user_default_mode,
    set_user_default_speed,
    set_user_default_voice,
    verify_and_claim_pairing_code,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_allowlisted_user_creates_row_and_persists_prefs(db_session, monkeypatch):
    """
    Allowlisted user in TELEGRAM_ALLOWED_USER_IDS in a private chat (chat_id == user_id)
    gets a persistent TelegramUser row with role='user', without becoming owner.
    """
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", "999001,999002")
    user_id = 999001
    chat_id = 999001

    assert not has_owner(db_session)

    # 1. Setting preference automatically creates row
    success = set_user_confirm_before_tts(db_session, user_id=user_id, chat_id=chat_id, enabled=True)
    assert success is True

    # 2. Row exists with role='user'
    user = db_session.query(TelegramUser).filter_by(telegram_user_id=user_id).first()
    assert user is not None
    assert user.role == "user"
    assert user.confirm_before_tts is True
    assert user.telegram_chat_id == chat_id

    # 3. Instance still has no owner (role='user' does not count as owner)
    assert not has_owner(db_session)

    # 4. Effective preferences return persisted value
    prefs = get_effective_user_preferences(db_session, user_id=user_id)
    assert prefs["confirm_before_tts"] is True

    # 5. Set other preferences
    assert set_user_default_voice(db_session, user_id, "af_bella", chat_id=chat_id) is True
    assert set_user_default_speed(db_session, user_id, 1.1, chat_id=chat_id) is True
    assert set_user_default_mode(db_session, user_id, "brief", chat_id=chat_id) is True

    db_session.refresh(user)
    assert user.default_voice == "af_bella"
    assert user.default_speed == 1.1
    assert user.default_mode == "brief"


def test_allowlisted_user_wrong_chat_rejected(db_session, monkeypatch):
    """Allowlisted user with mismatching chat_id is rejected and no row is created."""
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", "999001")
    user_id = 999001
    wrong_chat_id = 888888

    # Setting preference fails
    success = set_user_confirm_before_tts(db_session, user_id=user_id, chat_id=wrong_chat_id, enabled=True)
    assert success is False

    user = db_session.query(TelegramUser).filter_by(telegram_user_id=user_id).first()
    assert user is None


def test_existing_row_wrong_chat_rejected(db_session, monkeypatch):
    """Existing paired user cannot have preferences modified from a different chat ID."""
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="real_owner")

    # Mismatching chat ID
    success = set_user_confirm_before_tts(db_session, user_id=12345, chat_id=99999, enabled=True)
    assert success is False

    user = db_session.query(TelegramUser).filter_by(telegram_user_id=12345).first()
    assert user.confirm_before_tts is False


def test_allowlisted_user_promoted_to_owner_on_pairing(db_session, monkeypatch):
    """
    If an allowlisted user with role='user' later pairs with a pairing code,
    their existing row is updated to role='owner' without duplicate row creation.
    """
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", "777001")
    user_id = 777001
    chat_id = 777001

    # Create allowlisted user row via preference
    set_user_confirm_before_tts(db_session, user_id=user_id, chat_id=chat_id, enabled=True)
    user = db_session.query(TelegramUser).filter_by(telegram_user_id=user_id).first()
    assert user.role == "user"
    assert not has_owner(db_session)

    # Now owner pairs using code
    code = generate_pairing_code(db_session)
    ok, msg = verify_and_claim_pairing_code(db_session, code, user_id=user_id, chat_id=chat_id, username="promoted_owner")
    assert ok is True
    assert "Pairing successful" in msg

    # Row is promoted
    db_session.refresh(user)
    assert user.role == "owner"
    assert user.confirm_before_tts is True  # Preserved preference
    assert has_owner(db_session)
    assert db_session.query(TelegramUser).count() == 1
