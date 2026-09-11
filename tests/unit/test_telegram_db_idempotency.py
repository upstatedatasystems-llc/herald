import uuid
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.core.models import HeraldRequest
from herald.core.pipeline import process_herald_request
from herald.db.models import Base, JobState, PodcastJob


def test_telegram_unique_constraint_and_race_handling(monkeypatch):
    """
    Test that concurrent requests with the same (transport, telegram_chat_id, telegram_message_id)
    only create one job and race attempts load the existing job safely.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    req1 = HeraldRequest(
        transport="telegram",
        transport_message_id="1001",
        requester_identity="telegram:888",
        delivery_target="999",
        request_mode="literal",
        source_text="Test source message for idempotency validation.",
    )

    with TestingSession() as db:
        res1 = process_herald_request(db, req1)
        assert res1.is_duplicate is False

        # Attempt to insert identical transport message
        req2 = HeraldRequest(
            transport="telegram",
            transport_message_id="1001",
            requester_identity="telegram:888",
            delivery_target="999",
            request_mode="literal",
            source_text="Test source message for idempotency validation.",
        )
        res2 = process_herald_request(db, req2)
        assert res2.is_duplicate is True
        assert res2.job_id == res1.job_id

        # Verify only 1 job exists in DB
        count = db.query(PodcastJob).count()
        assert count == 1


class TestTelegramSessionRecovery:
    """Verify session rollback and provisional job recovery."""

    def test_exception_triggers_rollback(self, db_session):
        """Exception during process_herald_request must trigger db.rollback()."""
        from herald.telegram.bot import handle_telegram_content_message

        client = MagicMock()
        message = {
            "chat": {"id": 99999, "type": "private"},
            "from": {"id": 88888},
            "message_id": 500,
            "text": "https://example.com/article",
        }

        with (
            patch("herald.telegram.bot.is_user_authorized", return_value=True),
            patch("herald.telegram.bot.has_owner", return_value=True),
            patch(
                "herald.telegram.bot.process_herald_request",
                side_effect=RuntimeError("Simulated crash"),
            ),
            patch("herald.telegram.bot.get_effective_user_preferences", return_value={}),
            patch.object(db_session, "rollback") as mock_rollback,
        ):
            handle_telegram_content_message(db_session, client, message)

        mock_rollback.assert_called_once()
        client.send_message.assert_called()
        call_args = client.send_message.call_args
        assert "could not process" in call_args.kwargs.get("text", call_args[1].get("text", "")).lower() or \
               "could not process" in str(call_args)


class TestProvisionalJobIntakeRecovery:
    """Verify provisional EXTRACTING job is recovered to FAILED_FINAL on unexpected intake crashes."""

    def test_provisional_job_recovered_on_crash(self, db_session):
        from herald.telegram.bot import handle_telegram_content_message

        chat_id = 77777
        msg_id = 888
        provisional = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            telegram_chat_id=chat_id,
            telegram_message_id=msg_id,
            request_mode="standard",
            source_type="url",
            source_hash="hash-prov-crash",
            source_text="provisional text",
            status=JobState.EXTRACTING.value,
        )
        db_session.add(provisional)
        db_session.commit()

        client = MagicMock()
        message = {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 12345},
            "message_id": msg_id,
            "text": "https://example.com/article",
        }

        with (
            patch("herald.telegram.bot.is_user_authorized", return_value=True),
            patch("herald.telegram.bot.has_owner", return_value=True),
            patch(
                "herald.telegram.bot.process_herald_request",
                side_effect=RuntimeError("Intake crash during extraction"),
            ),
            patch("herald.telegram.bot.get_effective_user_preferences", return_value={}),
            patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"),
        ):
            handle_telegram_content_message(db_session, client, message)

        db_session.refresh(provisional)
        assert provisional.status == JobState.FAILED_FINAL.value
        assert provisional.error_code == "INTAKE_CRASH"
        assert provisional.failed_stage == "EXTRACTION"

        from herald.db.models import JobDiagnosticEvent

        event = (
            db_session.query(JobDiagnosticEvent)
            .filter_by(job_id=provisional.id, event_type="UNEXPECTED_INTAKE_FAILURE")
            .first()
        )
        assert event is not None
        assert event.metadata_json_sanitized.get("prior_state") == "EXTRACTING"
        assert event.metadata_json_sanitized.get("failure_stage") == "EXTRACTION"
        assert event.metadata_json_sanitized.get("error_category") == "INTAKE_CRASH"


def test_transport_idempotency_for_completed_jobs(db_session, tmp_path):
    """
    Verify:
    - When an exact transport replay of a COMPLETE job arrives (same telegram message),
      Herald does NOT send audio or redelivery messages to the user.
    """
    audio_file = tmp_path / "test_audio.mp3"
    audio_file.write_bytes(b"ID3mock_audio_content")

    job = PodcastJob(
        id="job-completed-idemp",
        transport="telegram",
        telegram_chat_id=54321,
        telegram_message_id=9876,
        status=JobState.COMPLETE.value,
        source_url="https://example.com/article",
        source_hash="hash-complete-idemp",
        source_text="Test source text content",
        local_audio_path=str(audio_file),
    )
    db_session.add(job)
    db_session.commit()

    from herald.telegram.bot import handle_telegram_content_message
    from herald.telegram.client import TelegramClient

    mock_client = MagicMock(spec=TelegramClient)
    mock_client.is_configured = True

    message_data = {
        "message_id": 9876,
        "chat": {"id": 54321, "type": "private"},
        "from": {"id": 111, "first_name": "Test"},
        "text": "https://example.com/article",
    }

    with patch("herald.telegram.bot.is_user_authorized", return_value=True):
        handle_telegram_content_message(
            db=db_session,
            client=mock_client,
            message=message_data,
        )

    mock_client.send_audio.assert_not_called()
    mock_client.send_message.assert_not_called()

