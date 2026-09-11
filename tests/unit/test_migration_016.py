import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from herald.db.models import JobState, PodcastJob


class TestMigration016:
    """Verify migration 016 expand custom_title to Text."""

    def test_migration_file_exists(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        path = repo_root / "migrations" / "versions" / "016_expand_custom_title_text.py"
        assert path.exists(), "Migration 016 file must exist"

    def test_migration_revision_chain(self):
        import importlib.util
        repo_root = Path(__file__).resolve().parent.parent.parent
        mig_path = repo_root / "migrations" / "versions" / "016_expand_custom_title_text.py"
        spec = importlib.util.spec_from_file_location(
            "migration_016",
            mig_path,
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "016_expand_custom_title_text"
        assert mod.down_revision == "015_rerun_lineage_diagnostics"

    def test_long_title_persists(self, db_session: Session):
        """Titles >255 characters should persist without truncation after migration."""
        long_title = "A" * 500
        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="api",
            source_hash="hash-long-title",
            source_text="test source",
            request_mode="standard",
            source_type="text",
            custom_title=long_title,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)
        assert len(job.custom_title) == 500
        assert job.custom_title == long_title

class TestMigration016DowngradeGuard:
    """Verify migration 016 downgrade safely rejects titles >255 chars."""

    def test_downgrade_fails_when_title_exceeds_255(self, monkeypatch):
        import importlib.util

        mock_op = MagicMock()
        mock_bind = MagicMock()
        mock_op.get_bind.return_value = mock_bind

        # First query: MAX(LENGTH(custom_title)) returns 300
        mock_max_res = MagicMock()
        mock_max_res.scalar.return_value = 300
        # Second query: COUNT(*) returns 2
        mock_count_res = MagicMock()
        mock_count_res.scalar.return_value = 2

        mock_bind.execute.side_effect = [mock_max_res, mock_count_res]

        p = Path("migrations/versions/016_expand_custom_title_text.py").resolve()
        spec = importlib.util.spec_from_file_location("mig_016", p)
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        monkeypatch.setattr(mig, "op", mock_op)

        with pytest.raises(Exception, match="Cannot downgrade: 2 custom_title values exceed 255 characters"):
            mig.downgrade()

    def test_downgrade_succeeds_when_all_titles_fit(self, monkeypatch):
        import importlib.util

        mock_op = MagicMock()
        mock_bind = MagicMock()
        mock_op.get_bind.return_value = mock_bind

        mock_max_res = MagicMock()
        mock_max_res.scalar.return_value = 200
        mock_bind.execute.return_value = mock_max_res

        p = Path("migrations/versions/016_expand_custom_title_text.py").resolve()
        spec = importlib.util.spec_from_file_location("mig_016", p)
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        monkeypatch.setattr(mig, "op", mock_op)

        mig.downgrade()
        mock_op.batch_alter_table.assert_called_once_with("podcast_jobs")

class TestLongTitleEndToEndBoundary:
    """Verify >255 character titles persist canonically without truncation, and formatters/slugs remain bounded."""

    def test_long_title_end_to_end_boundary(self, db_session: Session):
        from herald.services.diagnostics_export import _sanitize_slug
        from herald.telegram.formatters import format_approval, format_completion, format_queued

        title_300 = "Comprehensive Deep Dive Into Advanced Neural Architectures and Transformer Optimizations in Production Systems " * 3
        assert len(title_300) > 300

        job = PodcastJob(
            id=str(uuid.uuid4()),
            transport="telegram",
            source_hash="hash-boundary-long-title",
            source_text="Full text source content for boundary verification.",
            request_mode="standard",
            source_type="text",
            custom_title=title_300,
            script_json={
                "episode_title": title_300,
                "episode_description": "Detailed multi-part podcast episode discussing modern deep learning.",
                "estimated_minutes": 10,
                "segments": [{"segment_title": "Intro", "narration": "Welcome to our discussion."}],
            },
            status=JobState.COMPLETE.value,
            audio_duration_seconds=600.0,
            local_audio_path="/tmp/test_boundary_audio.mp3",
            created_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        # 1. Complete canonical title persists in DB without truncation
        assert job.custom_title == title_300
        assert len(job.custom_title) > 255

        # 2. Telegram formatters remain bounded
        caption = format_completion(job, actual_chunks_count=5, file_size_bytes=8_000_000)
        assert len(caption) <= 1024, f"Telegram audio caption must be <= 1024 characters, got {len(caption)}"
        assert "..." in caption, "Long title should be safely truncated in presentation"

        queued_text = format_queued(job, script_json=job.script_json)
        assert len(queued_text) <= 4096, "Queued card text must fit in Telegram message limit"

        approval_text, markup = format_approval(job, script_json=job.script_json)
        assert len(approval_text) <= 4096, "Approval card text must fit in Telegram message limit"

        # 3. Diagnostic filename / slug remains safe and bounded <= 32 chars
        slug = _sanitize_slug(job.custom_title)
        assert len(slug) <= 32
        assert not any(c in slug for c in r'<>:"/\|?*')

    def test_long_title_request_pipeline_regression(self, db_session: Session):
        """End-to-end regression: HeraldRequest with >255-character custom title through process_herald_request."""
        from herald.core.models import HeraldRequest
        from herald.core.pipeline import process_herald_request
        from herald.services.diagnostics_export import _sanitize_slug
        from herald.telegram.formatters import format_approval, format_completion, format_queued

        title_300 = ("Comprehensive Deep Dive Into Advanced Neural Architectures and Transformer Optimizations in Production Systems " * 3).strip()
        assert len(title_300) > 300

        req = HeraldRequest(
            transport="telegram",
            transport_message_id=777,
            requester_identity="telegram:777",
            delivery_target="777",
            request_mode="literal",
            source_text="This is valid source text of sufficient length to generate a complete script and podcast episode for the test.",
            custom_title=title_300,
        )

        with patch("herald.services.diagnostics_export.ensure_terminal_diagnostics_archive"):
            resp = process_herald_request(db=db_session, req=req)

        assert resp.status in (JobState.QUEUED_TTS.value, JobState.COMPLETE.value, JobState.AWAITING_APPROVAL.value)
        job = db_session.query(PodcastJob).filter(PodcastJob.id == resp.job_id).first()
        assert job is not None
        # 1. Complete canonical custom_title persists in DB without truncation
        assert job.custom_title == title_300
        assert len(job.custom_title) > 255

        # 2. Session remains fully usable
        db_session.refresh(job)
        assert job.status in (JobState.QUEUED_TTS.value, JobState.COMPLETE.value, JobState.AWAITING_APPROVAL.value)

        # 3. Telegram formatters remain safely bounded
        caption = format_completion(job, actual_chunks_count=1, file_size_bytes=1_000_000)
        assert len(caption) <= 1024
        assert "..." in caption

        queued_text = format_queued(job, script_json=job.script_json)
        assert len(queued_text) <= 4096

        approval_text, _ = format_approval(job, script_json=job.script_json)
        assert len(approval_text) <= 4096

        # 4. Diagnostic slug remains safe
        slug = _sanitize_slug(title_300)
        assert len(slug) <= 32

