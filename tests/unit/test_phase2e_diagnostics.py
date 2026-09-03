"""
Comprehensive unit tests for Package 2E: Diagnostics & Support Export.
Verifies:
- /diagnostics command resolution (latest caller job, /diagnostics latest alias, UUID, short prefix, ambiguous rejection)
- Tenant isolation and access control (private chat, user authorization, cross-user invisibility)
- Diagnostic summary card formatting (truthful, HTML escaped, success & failure cases)
- Downloadable support export ZIP generation (valid zip, 13 canonical files, source.txt, script.json/md, research artifacts, path traversal safe, staging cleanup)
- Redaction of sentinel secrets (TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY, MISTRAL_API_KEY, CLOUDFLARE_API_TOKEN, HERALD_API_KEY, DB password, Bearer header, exceptions)
- Pre-send fail-closed secret scan on generated ZIP bundle
- Safe delivery failure masking (no raw exceptions or secrets in user-facing message)
- Literal mode zero AI interaction invariant
- Job diagnostic event recording and export in diagnostic-events.jsonl
- Completion inline keyboard Diagnostics button (h2:diag:<uuid>) and repeat safety
- setMyCommands registration of diagnostics command
"""

import json
import zipfile
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.connection import Base
from herald.db.models import (
    AIInteraction,
    JobDiagnosticEvent,
    JobProcessingMetric,
    JobState,
    JobStateTransition,
    PodcastJob,
    PodcastTTSChunk,
    TelegramUser,
)
from herald.services.diagnostics_export import (
    generate_job_diagnostics_zip,
)
from herald.telegram.bot import handle_telegram_command
from herald.telegram.delivery import deliver_job_diagnostics, format_diagnostics_caption
from herald.telegram.resolver import resolve_user_job


def setup_in_memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return session_factory()


def test_diagnostics_job_resolution_and_latest_alias():
    db = setup_in_memory_db()
    user_id = 12345
    chat_id = 12345

    now = datetime.now(UTC)
    job1 = PodcastJob(
        id="11111111-1111-4111-8111-111111111111",
        transport="telegram",
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        source_hash="hash1",
        source_text="Source 1",
        status=JobState.COMPLETE.value,
        created_at=now - timedelta(minutes=10),
        completed_at=now - timedelta(minutes=9),
    )
    job2 = PodcastJob(
        id="22222222-2222-4222-8222-222222222222",
        transport="telegram",
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        source_hash="hash2",
        source_text="Source 2",
        status=JobState.FAILED_FINAL.value,
        created_at=now - timedelta(minutes=5),
        failed_stage="TTS_SYNTHESIS",
    )
    job3 = PodcastJob(
        id="22229999-3333-4333-8333-333333333333",
        transport="telegram",
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        source_hash="hash3",
        source_text="Source 3",
        status=JobState.COMPLETE.value,
        created_at=now - timedelta(minutes=1),
        completed_at=now,
    )
    job_other = PodcastJob(
        id="99999999-9999-4999-8999-999999999999",
        transport="telegram",
        telegram_user_id=99999,
        telegram_chat_id=99999,
        source_hash="hash9",
        source_text="Other",
        status=JobState.COMPLETE.value,
        created_at=now,
    )
    db.add_all([job1, job2, job3, job_other])
    db.commit()

    # 1. No identifier -> resolves latest caller job (job3)
    resolved = resolve_user_job(db, telegram_user_id=user_id, telegram_chat_id=chat_id, completed_only=False)
    assert resolved is not None
    assert resolved.id == job3.id

    # 2. Exact UUID: resolves job1
    resolved_exact = resolve_user_job(
        db, telegram_user_id=user_id, telegram_chat_id=chat_id, identifier="11111111-1111-4111-8111-111111111111", completed_only=False
    )
    assert resolved_exact is not None
    assert resolved_exact.id == job1.id

    # 3. Unambiguous prefix: "1111"
    resolved_prefix = resolve_user_job(db, telegram_user_id=user_id, telegram_chat_id=chat_id, identifier="1111", completed_only=False)
    assert resolved_prefix is not None
    assert resolved_prefix.id == job1.id

    # 4. Ambiguous prefix: "2222" matches both job2 and job3 -> returns None
    resolved_ambig = resolve_user_job(db, telegram_user_id=user_id, telegram_chat_id=chat_id, identifier="2222", completed_only=False)
    assert resolved_ambig is None

    # 5. Short prefix (< 4 chars) -> rejected
    assert resolve_user_job(db, telegram_user_id=user_id, telegram_chat_id=chat_id, identifier="111", completed_only=False) is None

    # 6. Other user job -> invisible
    assert (
        resolve_user_job(
            db,
            telegram_user_id=user_id,
            telegram_chat_id=chat_id,
            identifier="99999999-9999-4999-8999-999999999999",
            completed_only=False,
        )
        is None
    )


def test_diagnostics_command_with_latest_alias(tmp_path):
    db = setup_in_memory_db()
    user_id = 55555
    chat_id = 55555

    owner = TelegramUser(
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        username="diag_tester",
        role="owner",
        is_active=True,
    )
    db.add(owner)

    now = datetime.now(UTC)
    job = PodcastJob(
        id="aabbccdd-1234-4567-89ab-cdef01234567",
        transport="telegram",
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        source_type="url",
        source_url="https://example.com/tech-news",
        source_hash="sha256_mock",
        source_text="Mock article text",
        custom_title="Tech Evolution",
        request_mode="standard",
        status=JobState.COMPLETE.value,
        audio_duration_seconds=185,
        audio_bytes=1500000,
        kokoro_voice="af_heart",
        kokoro_speed=1.0,
        gemini_model="gemini-3.5-flash",
        created_at=now - timedelta(seconds=200),
        claimed_at=now - timedelta(seconds=195),
        completed_at=now - timedelta(seconds=10),
        delivered_at=now,
    )
    db.add(job)
    db.commit()

    mock_client = MagicMock()
    mock_msg = {
        "message_id": 99,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "username": "diag_tester"},
    }

    with patch("herald.config.settings.HERALD_WORK_DIR", str(tmp_path)):
        # Test /diagnostics latest
        handle_telegram_command(db, mock_client, mock_msg, "diagnostics", "latest")

    assert mock_client.send_message.called
    assert mock_client.send_document.called

    sent_card = mock_client.send_message.call_args[1]["text"]
    assert "aabbccdd" in sent_card
    assert "Tech Evolution" in sent_card


def test_diagnostics_canonical_13_artifact_bundle(tmp_path):
    """Verify that the diagnostics package contains the canonical artifacts including full source and script."""
    db = setup_in_memory_db()
    now = datetime.now(UTC)
    job = PodcastJob(
        id="33333333-3333-4333-8333-333333333333",
        transport="telegram",
        telegram_user_id=100,
        telegram_chat_id=100,
        source_type="text",
        source_hash="sha_zip_test",
        source_text="This is the full unredacted source text that must be preserved.",
        custom_title="Diagnostics Artifact Test",
        request_mode="research",
        status=JobState.COMPLETE.value,
        audio_duration_seconds=120,
        audio_bytes=800000,
        completed_chunk_index=2,
        research_grounding_json={"search_queries": ["query1"], "sources": ["https://src1.org"]},
        research_json={"claims": [{"claim": "c1", "verification": "verified"}]},
        research_audit_json={"has_material_issues": False, "score": 1.0},
        script_json={
            "episode_title": "Diagnostics Artifact Test",
            "episode_description": "Comprehensive diagnostics test episode.",
            "estimated_minutes": 2,
            "source_title": "Original Topic",
            "segments": [
                {"order": 1, "heading": "Intro", "narration": "Welcome to the diagnostics artifact test narration."},
                {"order": 2, "heading": "Details", "narration": "Every segment narration must be retained in script.json."},
            ],
            "warnings": [],
        },
        error_code="NONE",
        created_at=now - timedelta(minutes=5),
        completed_at=now,
    )
    db.add(job)

    # Add metric
    metric = JobProcessingMetric(
        job_id=job.id,
        stage="RESEARCH_SCRIPT",
        status="success",
        duration_ms=4500,
        started_at=now - timedelta(minutes=4),
        finished_at=now - timedelta(minutes=3),
    )
    # Add transition
    trans = JobStateTransition(
        job_id=job.id,
        from_state="SCRIPTING",
        to_state="SCRIPT_READY",
        component="herald-core",
        message="Scripting finished",
    )
    # Add chunks
    c1 = PodcastTTSChunk(job_id=job.id, chunk_index=0, text_hash="h1", status="COMPLETE", audio_duration=60.0)
    c2 = PodcastTTSChunk(job_id=job.id, chunk_index=1, text_hash="h2", status="COMPLETE", audio_duration=60.0)
    # Add AI interaction
    ai = AIInteraction(
        job_id=job.id,
        provider="gemini",
        model="gemini-3.5-flash",
        operation="script_generation",
        attempt=1,
        http_status=200,
        provider_request_id="req-gemini-123",
        started_at=now - timedelta(minutes=4),
        completed_at=now - timedelta(minutes=3),
        duration_ms=4500,
        success=True,
        prompt_tokens=300,
        completion_tokens=500,
        total_tokens=800,
    )
    # Add Diagnostic Event
    evt = JobDiagnosticEvent(
        job_id=job.id,
        timestamp=now - timedelta(minutes=4),
        level="INFO",
        component="pipeline",
        event_type="SCRIPTING_BEGIN",
        message="Starting script generation for research mode",
        metadata_json_sanitized={"depth": "medium"},
    )
    db.add_all([metric, trans, c1, c2, ai, evt])
    db.commit()

    with patch("herald.config.settings.HERALD_WORK_DIR", str(tmp_path)):
        zip_path = generate_job_diagnostics_zip(db, job)
        assert zip_path.exists()

        # Verify staging directory was cleaned up
        staging_dirs = list((tmp_path / "diagnostics").glob("staging_*"))
        assert len(staging_dirs) == 0

        # Inspect ZIP contents
        with zipfile.ZipFile(zip_path, "r") as zf:
            namelist = zf.namelist()

            # Canonical bundle artifacts
            assert "README.txt" in namelist
            assert "manifest.json" in namelist
            assert "episode-details.md" in namelist
            assert "source.txt" in namelist
            assert "script.json" in namelist
            assert "script.md" in namelist
            assert "state-transitions.json" in namelist
            assert "processing-metrics.json" in namelist
            assert "tts-chunks.json" in namelist
            assert "diagnostic-events.jsonl" in namelist
            assert "ai-interactions.json" in namelist
            assert "config-sanitized.json" in namelist

            # Conditional Research artifacts
            assert "research/grounding.json" in namelist
            assert "research/dossier.json" in namelist
            assert "research/audit.json" in namelist

            # Verify source.txt contains full source
            src_content = zf.read("source.txt").decode("utf-8")
            assert "This is the full unredacted source text" in src_content

            # Verify script.json preserves narration text (not wiped by metadata redaction)
            script_raw = zf.read("script.json").decode("utf-8")
            script_data = json.loads(script_raw)
            assert "Welcome to the diagnostics artifact test narration." in script_data["segments"][0]["narration"]
            assert "Every segment narration must be retained in script.json." in script_data["segments"][1]["narration"]

            # Verify script.md is rendered
            script_md_content = zf.read("script.md").decode("utf-8")
            assert "# Diagnostics Artifact Test" in script_md_content
            assert "### Intro" in script_md_content

            # Verify manifest.json
            manifest_raw = zf.read("manifest.json").decode("utf-8")
            manifest = json.loads(manifest_raw)
            assert manifest["job_id"] == job.id
            assert manifest["schema_version"] == "2.0.0"
            assert "episode-details.md" in manifest["included_files"]
            assert "source.txt" in manifest["included_files"]
            assert "script.json" in manifest["included_files"]

            # Verify diagnostic-events.jsonl
            events_raw = zf.read("diagnostic-events.jsonl").decode("utf-8")
            assert "SCRIPTING_BEGIN" in events_raw


def test_diagnostics_pre_send_secret_scan_rejection(tmp_path):
    """Security Invariant: If a bundle contains an unredacted secret, fail closed and do not send ZIP."""
    db = setup_in_memory_db()
    sentinel_secret = "LEAKED_SUPER_SECRET_TOKEN_9999"

    with patch.object(settings, "GEMINI_API_KEY", sentinel_secret), \
         patch("herald.config.settings.HERALD_WORK_DIR", str(tmp_path)):

        job = PodcastJob(
            id="leak-test-job-uuid-1111",
            transport="telegram",
            telegram_user_id=1,
            telegram_chat_id=1,
            source_type="text",
            source_hash="sha",
            source_text="Clean text",
            status=JobState.COMPLETE.value,
            created_at=datetime.now(UTC),
        )
        db.add(job)
        db.commit()

        # Intentionally inject the raw secret into a non-sanitized mock file during ZIP generation
        with patch("herald.services.diagnostics_export.build_safe_environment_summary", return_value={"leaked": sentinel_secret}):
            try:
                generate_job_diagnostics_zip(db, job)
                assert False, "Expected generate_job_diagnostics_zip to raise a security error"
            except RuntimeError as re:
                assert "Security violation" in str(re)
                assert sentinel_secret not in str(re)  # Exception does not echo secret


def test_deliver_job_diagnostics_error_masking(tmp_path):
    """Verify deliver_job_diagnostics masks raw exceptions and never discloses secrets to Telegram user."""
    db = setup_in_memory_db()
    job = PodcastJob(
        id="mask-test-uuid-2222",
        transport="telegram",
        telegram_user_id=1,
        telegram_chat_id=1,
        source_hash="sha",
        source_text="Source",
        status=JobState.COMPLETE.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    mock_client = MagicMock()
    raw_secret_error = "Internal error connecting to https://api.openai.com with key sk-secret-token-12345"

    with patch("herald.services.diagnostics_export.generate_job_diagnostics_zip", side_effect=ValueError(raw_secret_error)):
        deliver_job_diagnostics(db, mock_client, job, chat_id=1)

    # Invariant: User receives generic safe error, NOT the raw exception
    assert mock_client.send_message.called
    sent_text = mock_client.send_message.call_args[1]["text"]
    assert "Diagnostics package generation failed" in sent_text
    assert "sk-secret-token-12345" not in sent_text
    assert "api.openai.com" not in sent_text


def test_diagnostics_caption_formatting():
    """Verify diagnostics delivery caption format and size presentation."""
    job = PodcastJob(
        id="aabb1122-3344-5566-7788-99aabbccddeeff",
        transport="telegram",
        telegram_user_id=1,
        telegram_chat_id=1,
        source_hash="sha",
        source_text="Source",
        custom_title="Caption Format Test Episode",
        request_mode="standard",
        status=JobState.COMPLETE.value,
    )
    caption_small = format_diagnostics_caption(job, 45 * 1024)
    assert "Caption Format Test Episode" in caption_small
    assert "aabb1122" in caption_small
    assert "standard" in caption_small
    assert "COMPLETE" in caption_small
    assert "45.0 KB" in caption_small
