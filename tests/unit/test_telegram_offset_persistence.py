from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base, TelegramPollState, TelegramUpdateFailure
from herald.telegram.bot import run_telegram_bot


def test_telegram_offset_persistence_and_poison_update_quarantine(monkeypatch):
    """
    Test that:
    1. Polling offset is loaded from and persisted to DB.
    2. Successfully processed update advances offset.
    3. Poison update is retried up to 3 times then dead-lettered, allowing polling to advance.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    import herald.telegram.bot as bot_mod

    monkeypatch.setattr(bot_mod, "SessionLocal", TestingSession)

    mock_client = MagicMock()
    mock_client.is_configured = True

    # Prepare 3 updates: Update 10 (good), Update 11 (poison), Update 12 (good)
    updates_batch_1 = [
        {
            "update_id": 10,
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 999},
                "text": "/help",
                "message_id": 1,
            },
        },
        {
            "update_id": 11,
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 999},
                "text": "/broken",
                "message_id": 2,
            },
        },
        {
            "update_id": 12,
            "message": {
                "chat": {"id": 12345, "type": "private"},
                "from": {"id": 999},
                "text": "/help",
                "message_id": 3,
            },
        },
    ]

    # Mock client.get_updates to return batch
    call_count = 0

    def mock_get_updates(offset, limit=50, timeout=10):
        nonlocal call_count
        call_count += 1
        if call_count <= 4:
            # Filter updates >= offset
            return [u for u in updates_batch_1 if u["update_id"] >= offset]
        # After 4 iterations, raise KeyboardInterrupt to exit loop
        raise KeyboardInterrupt()

    mock_client.get_updates = mock_get_updates
    monkeypatch.setattr(bot_mod, "TelegramClient", lambda: mock_client)

    # Mock handle_telegram_command to fail specifically on /broken
    def mock_handle_command(db, client, message, cmd, args):
        if cmd == "broken":
            raise RuntimeError("Poison pill command failure!")
        # For other commands, do nothing

    monkeypatch.setattr(bot_mod, "handle_telegram_command", mock_handle_command)
    monkeypatch.setattr(bot_mod, "deliver_pending_telegram_jobs", lambda db, client: 0)

    try:
        run_telegram_bot(poll_interval=0.01)
    except KeyboardInterrupt:
        pass

    # Verify final DB state
    with TestingSession() as db:
        poll_state = db.query(TelegramPollState).first()
        assert poll_state is not None
        # Offset should have advanced past update 11 and processed update 12
        assert poll_state.last_processed_update_id == 12

        # Check failure record for update 11
        fail_rec = db.query(TelegramUpdateFailure).filter_by(update_id=11).first()
        assert fail_rec is not None
        assert fail_rec.attempt_count >= 3
        assert fail_rec.is_dead_lettered is True
        assert "Poison pill" in (fail_rec.last_error or "")
