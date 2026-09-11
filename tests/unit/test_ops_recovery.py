import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy.orm import Session

from herald.db.models import JobState, PodcastJob
from herald.services.recovery import ops_stale_recovery


class TestStaleExtractingRecoveryNarrowing:
    """Verify stale EXTRACTING recovery for Telegram intake jobs."""

    def test_api_extracting_job_not_recovered(self, db_session: Session):
        """API-transport EXTRACTING jobs should NOT be recovered by stale recovery."""

        stale_time = datetime.now(UTC) - timedelta(minutes=30)
        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="api",
            source_hash="hash-api-stale",
            source_text="test source",
            request_mode="standard",
            source_type="text",
            status=JobState.EXTRACTING.value,
            claimed_at=stale_time,
            last_heartbeat_at=stale_time,
        )
        db_session.add(job)
        db_session.commit()

        with patch("herald.services.recovery.ensure_terminal_diagnostics_archive"):
            result = ops_stale_recovery(db=db_session)

        db_session.refresh(job)
        assert job.status == JobState.EXTRACTING.value, "API EXTRACTING job should not be recovered"
        assert result["recovered_jobs"] == 0

    def test_stale_telegram_no_heartbeat_recovered_to_failed_final(self, db_session: Session):
        """Stale Telegram EXTRACTING job with NO claim and NO heartbeat must become FAILED_FINAL."""

        stale_time = datetime.now(UTC) - timedelta(minutes=30)
        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            telegram_chat_id=12345,
            telegram_message_id=999,
            source_hash="hash-tg-no-heartbeat",
            source_text="",
            request_mode="standard",
            source_type="url",
            status=JobState.EXTRACTING.value,
            created_at=stale_time,
            updated_at=stale_time,
            claimed_at=None,
            last_heartbeat_at=None,
        )
        db_session.add(job)
        db_session.commit()

        with patch("herald.services.recovery.ensure_terminal_diagnostics_archive") as mock_archive:
            result = ops_stale_recovery(db=db_session)

        db_session.refresh(job)
        assert job.status == JobState.FAILED_FINAL.value, "Stale no-heartbeat Telegram job must transition to FAILED_FINAL"
        assert job.error_code == "INTAKE_TIMEOUT"
        assert job.failed_stage == "EXTRACTING"
        assert result["recovered_jobs"] >= 1
        mock_archive.assert_called_with(job.id, JobState.FAILED_FINAL.value)

    def test_recent_telegram_no_heartbeat_unchanged(self, db_session: Session):
        """Recent Telegram EXTRACTING job with no claim/heartbeat should remain untouched."""

        recent_time = datetime.now(UTC) - timedelta(minutes=3)
        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            telegram_chat_id=12345,
            telegram_message_id=998,
            source_hash="hash-tg-recent",
            source_text="",
            request_mode="standard",
            source_type="url",
            status=JobState.EXTRACTING.value,
            created_at=recent_time,
            updated_at=recent_time,
            claimed_at=None,
            last_heartbeat_at=None,
        )
        db_session.add(job)
        db_session.commit()

        with patch("herald.services.recovery.ensure_terminal_diagnostics_archive"):
            ops_stale_recovery(db=db_session)

        db_session.refresh(job)
        assert job.status == JobState.EXTRACTING.value, "Recent Telegram job must not be recovered"

    def test_row_advances_before_recovery_not_overwritten(self, db_session: Session):
        """If a row advances to another status, stale recovery skips it without overwriting."""

        stale_time = datetime.now(UTC) - timedelta(minutes=30)
        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            telegram_chat_id=12345,
            telegram_message_id=997,
            source_hash="hash-tg-advanced",
            source_text="complete source",
            request_mode="standard",
            source_type="url",
            status=JobState.COMPLETE.value,
            created_at=stale_time,
            updated_at=stale_time,
            claimed_at=None,
            last_heartbeat_at=None,
        )
        db_session.add(job)
        db_session.commit()

        with patch("herald.services.recovery.ensure_terminal_diagnostics_archive") as mock_archive:
            ops_stale_recovery(db=db_session)

        db_session.refresh(job)
        assert job.status == JobState.COMPLETE.value
        mock_archive.assert_not_called()
