from datetime import UTC, datetime, timedelta

from herald.db.models import TelegramPairingCode
from herald.telegram.auth import (
    generate_pairing_code,
    get_paired_owner,
    has_owner,
    is_user_authorized,
    verify_and_claim_pairing_code,
)


def test_pairing_code_lifecycle(db_session):
    """Test generating, validating, and claiming pairing codes."""
    assert not has_owner(db_session)
    assert get_paired_owner(db_session) is None

    # Generate pairing code
    code = generate_pairing_code(db_session, expires_in_minutes=10)
    assert len(code) == 6
    assert code.isdigit()

    # Unauthorized user check
    assert not is_user_authorized(db_session, 12345678)

    # Point 3: Pairing succeeds
    success, msg = verify_and_claim_pairing_code(
        db=db_session,
        code=code,
        user_id=12345678,
        chat_id=998877,
        username="podcast_owner",
        first_name="Alice",
    )
    assert success is True
    assert "Pairing successful" in msg

    # Verify owner in DB
    owner = get_paired_owner(db_session)
    assert owner is not None
    assert owner.telegram_user_id == 12345678
    assert owner.username == "podcast_owner"
    assert is_user_authorized(db_session, 12345678, 998877)

    # Point 4: Pairing code cannot be reused / owner already paired
    success2, msg2 = verify_and_claim_pairing_code(
        db=db_session,
        code=code,
        user_id=87654321,
        chat_id=112233,
        username="intruder",
    )
    assert success2 is False
    assert "Invalid or expired" in msg2 or "already paired" in msg2


def test_expired_pairing_code_rejected(db_session):
    """Test expired pairing code is rejected."""
    now = datetime.now(UTC)
    expired_code = TelegramPairingCode(
        code="999999",
        is_used=False,
        expires_at=now - timedelta(minutes=5),
        created_at=now - timedelta(minutes=20),
    )
    db_session.add(expired_code)
    db_session.commit()

    success, msg = verify_and_claim_pairing_code(
        db=db_session,
        code="999999",
        user_id=55555,
        chat_id=55555,
    )
    assert success is False
    assert "Invalid or expired" in msg
