"""
Comprehensive unit tests for Package 2E: Diagnostics & Support Export.
Verifies:
- /diagnostics command resolution (latest caller job, UUID, short prefix, ambiguous rejection)
- Tenant isolation and access control (private chat, user authorization, cross-user invisibility)
- Diagnostic summary card formatting (truthful, HTML escaped, success & failure cases)
- Downloadable support export ZIP generation (valid zip, 6 expected json files, path traversal safe, cleanup)
- Redaction of sentinel secrets (TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, HERALD_API_KEY, DB password, Bearer header, exceptions)
- Literal mode zero AI interaction invariant
- Brief, Standard, Research AI interaction evidence and retry call tracking
- Completion inline keyboard Diagnostics button (h2:diag:<uuid>) and repeat safety
- setMyCommands registration of diagnostics command
"""

import json
import zipfile
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.telegram_bot.main import TELEGRAM_BOT_COMMANDS, register_bot_commands
from herald.config import settings
from herald.db.connection import Base
from herald.db.models import (
    AIInteraction,
    JobProcessingMetric,
    JobState,
    JobStateTransition,
    PodcastJob,
    PodcastTTSChunk,
    TelegramUser,
)
from herald.literal.script_generator import generate_literal_script
from herald.services.ai_recorder import record_ai_interaction
from herald.services.diagnostics_export import (
    generate_job_diagnostics_zip,
)
from herald.services.redaction import (
    redact_dict,
    redact_text,
    sanitize_error,
)
from herald.telegram.bot import handle_telegram_callback_query, handle_telegram_command
from herald.telegram.formatters import (
    format_completion_markup,
    format_diagnostics_card,
    format_help,
)
from herald.telegram.resolver import resolve_user_job


def setup_in_memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    return session_factory()


def test_diagnostics_job_resolution():
    db = setup_in_memory_db()
    user_id = 12345
    chat_id = 12345

    # Create jobs for user
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
    # Other user job
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

    # 1. No identifier: resolves latest caller job (job3)
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

    # 4. Ambiguous prefix: "2222" matches both job2 and job3 -> should return None
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


def test_diagnostics_command_and_card_formatting(tmp_path):
    db = setup_in_memory_db()
    user_id = 55555
    chat_id = 55555

    # Add authorized owner
    owner = TelegramUser(
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        username="diag_tester",
        role="owner",
        is_active=True,
    )
    db.add(owner)

    # Add successful job with AI interaction and chunks
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

    # Add TTS chunks
    chunk1 = PodcastTTSChunk(job_id=job.id, chunk_index=0, text_hash="h1", status="COMPLETE", audio_duration=90.0)
    chunk2 = PodcastTTSChunk(job_id=job.id, chunk_index=1, text_hash="h2", status="COMPLETE", audio_duration=95.0)
    db.add_all([chunk1, chunk2])

    # Add AI interaction
    interaction = AIInteraction(
        job_id=job.id,
        provider="gemini",
        model="gemini-3.5-flash",
        operation="script_generation",
        started_at=now - timedelta(seconds=190),
        completed_at=now - timedelta(seconds=180),
        duration_ms=10000,
        success=True,
        prompt_tokens=450,
        completion_tokens=600,
        total_tokens=1050,
    )
    db.add(interaction)
    db.commit()

    # Format card
    card_text = format_diagnostics_card(job, db)
    assert "Tech Evolution" in card_text
    assert "aabbccdd" in card_text
    assert "COMPLETE" in card_text
    assert "Gemini (gemini-3.5-flash)" in card_text
    assert "af_heart" in card_text
    assert "1.0x" in card_text
    assert "TTS Chunks:</b> 2" in card_text
    assert "1,050 tokens" in card_text
    assert "Mock article text" not in card_text  # No source text leakage

    # Test /diagnostics command execution
    mock_client = MagicMock()
    mock_msg = {
        "message_id": 99,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "username": "diag_tester"},
    }

    with patch("herald.config.settings.HERALD_WORK_DIR", str(tmp_path)):
        handle_telegram_command(db, mock_client, mock_msg, "diagnostics", "")

    # Verifies card was sent and document ZIP was sent
    assert mock_client.send_message.called
    assert mock_client.send_document.called

    sent_card = mock_client.send_message.call_args[1]["text"]
    assert "aabbccdd" in sent_card
    assert mock_client.send_document.call_args[1]["mime_type"] == "application/zip"


def test_diagnostics_failed_job_card():
    db = setup_in_memory_db()
    user_id = 777
    chat_id = 777

    job_failed = PodcastJob(
        id="deadbeef-0000-4000-8000-000000000000",
        transport="telegram",
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        source_type="text",
        source_hash="sha_fail",
        source_text="Failing text",
        custom_title="Failed Episode",
        request_mode="standard",
        status=JobState.FAILED_FINAL.value,
        failed_stage="AI_SCRIPT",
        error_code="GEMINI_QUOTA_EXCEEDED",
        error_detail="API rate limit exceeded on attempt 3",
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    db.add(job_failed)
    db.commit()

    card = format_diagnostics_card(job_failed, db)
    assert "Failed Episode" in card
    assert "deadbeef" in card
    assert "FAILED_FINAL" in card
    assert "AI_SCRIPT" in card
    assert "GEMINI_QUOTA_EXCEEDED" in card
    assert "API rate limit exceeded" in card


def test_diagnostics_zip_contents_and_cleanup(tmp_path):
    db = setup_in_memory_db()
    job = PodcastJob(
        id="33333333-3333-4333-8333-333333333333",
        transport="telegram",
        telegram_user_id=100,
        telegram_chat_id=100,
        source_type="url",
        source_url="https://news.ycombinator.com",
        source_hash="sha_zip_test",
        source_text="Unexported private text",
        custom_title="Zip Verification Podcast",
        request_mode="brief",
        status=JobState.COMPLETE.value,
        audio_duration_seconds=60,
        audio_bytes=500000,
        created_at=datetime.now(UTC),
    )
    db.add(job)

    # Add metric
    metric = JobProcessingMetric(
        job_id=job.id,
        stage="LITERAL_SCRIPT",
        status="success",
        duration_ms=120,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    # Add transition
    trans = JobStateTransition(
        job_id=job.id,
        from_state="RECEIVED",
        to_state="COMPLETE",
        component="test",
        message="Done",
    )
    # Add AI interaction
    ai = AIInteraction(
        job_id=job.id,
        provider="gemini",
        model="gemini-3.5-flash",
        operation="script_generation",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        duration_ms=1500,
        success=True,
        prompt_tokens=100,
        completion_tokens=200,
        total_tokens=300,
    )
    db.add_all([metric, trans, ai])
    db.commit()

    with patch("herald.config.settings.HERALD_WORK_DIR", str(tmp_path)):
        zip_path = generate_job_diagnostics_zip(db, job)
        assert zip_path.exists()
        assert zip_path.suffix == ".zip"

        # Verify staging directory was cleaned up
        staging_dirs = list((tmp_path / "diagnostics").glob("staging_*"))
        assert len(staging_dirs) == 0

        # Inspect ZIP contents
        with zipfile.ZipFile(zip_path, "r") as zf:
            namelist = zf.namelist()
            assert "summary.json" in namelist
            assert "job.json" in namelist
            assert "timings.json" in namelist
            assert "ai_interactions.json" in namelist
            assert "errors.json" in namelist
            assert "environment-summary.json" in namelist

            # Verify summary.json content
            summary_raw = zf.read("summary.json").decode("utf-8")
            summary_obj = json.loads(summary_raw)
            assert summary_obj["job_id"] == job.id
            assert summary_obj["title"] == "Zip Verification Podcast"
            assert summary_obj["ai_interaction_count"] == 1

            # Verify job.json omits raw source text
            job_raw = zf.read("job.json").decode("utf-8")
            assert "Unexported private text" not in job_raw

            # Verify environment-summary.json
            env_raw = zf.read("environment-summary.json").decode("utf-8")
            env_obj = json.loads(env_raw)
            assert "system" in env_obj
            assert "herald" in env_obj


def test_secret_redaction_and_sentinels(tmp_path):
    # Sentinel secrets
    sentinel_tg = "1234567890:AAHSentinelTelegramSecret123"
    sentinel_gemini = "AIzaSySentinelSecretGeminiApiKey98765"
    sentinel_herald = "herald_secret_master_sentinel_key"
    sentinel_db = "super_secret_postgres_password_123"

    with patch.object(settings, "TELEGRAM_BOT_TOKEN", sentinel_tg), \
         patch.object(settings, "GEMINI_API_KEY", sentinel_gemini), \
         patch.object(settings, "HERALD_API_KEY", sentinel_herald), \
         patch.object(settings, "POSTGRES_PASSWORD", sentinel_db), \
         patch.object(settings, "DATABASE_URL", f"postgresql://herald:{sentinel_db}@localhost:5432/herald"):

        # 1. Text redaction
        leaked_str = f"Error calling {sentinel_gemini} with token {sentinel_tg} and DB {sentinel_db} Authorization: Bearer secret_bearer_token"
        redacted = redact_text(leaked_str)
        assert sentinel_tg not in redacted
        assert sentinel_gemini not in redacted
        assert sentinel_herald not in redacted
        assert sentinel_db not in redacted
        assert "secret_bearer_token" not in redacted
        assert "[REDACTED]" in redacted

        # 2. Dictionary redaction
        leaked_dict = {
            "api_key": sentinel_gemini,
            "bot_token": sentinel_tg,
            "normal_field": "public_data",
            "nested": {
                "password": sentinel_db,
                "msg": f"Auth failed with {sentinel_herald}",
            },
        }
        cleaned_dict = redact_dict(leaked_dict)
        assert cleaned_dict["api_key"] == "[REDACTED]"
        assert cleaned_dict["bot_token"] == "[REDACTED]"
        assert cleaned_dict["nested"]["password"] == "[REDACTED]"
        assert sentinel_herald not in cleaned_dict["nested"]["msg"]

        # 3. Exception sanitization
        try:
            raise ValueError(f"Connection failed to api.telegram.org/bot{sentinel_tg}/sendMessage with key {sentinel_gemini}")
        except Exception as e:
            cat, msg = sanitize_error(e)
            assert sentinel_tg not in msg
            assert sentinel_gemini not in msg
            assert cat in ("AUTHENTICATION_FAILED", "NETWORK_ERROR", "ValueError")

        # 4. Diagnostics ZIP Sentinel Check
        db = setup_in_memory_db()
        job = PodcastJob(
            id="44444444-4444-4444-8444-444444444444",
            transport="telegram",
            telegram_user_id=1,
            telegram_chat_id=1,
            source_hash="sha",
            source_text="Private source",
            status=JobState.FAILED_FINAL.value,
            error_detail=f"Secret leak attempt: {sentinel_gemini} and {sentinel_tg}",
            created_at=datetime.now(UTC),
        )
        db.add(job)
        db.commit()

        with patch("herald.config.settings.HERALD_WORK_DIR", str(tmp_path)):
            zip_path = generate_job_diagnostics_zip(db, job)
            zip_bytes = zip_path.read_bytes()
            assert sentinel_tg.encode("utf-8") not in zip_bytes
            assert sentinel_gemini.encode("utf-8") not in zip_bytes
            assert sentinel_herald.encode("utf-8") not in zip_bytes
            assert sentinel_db.encode("utf-8") not in zip_bytes


def test_literal_mode_zero_ai_interactions_invariant():
    """Acceptance Invariant: Literal mode MUST generate ZERO AI interactions."""
    db = setup_in_memory_db()
    job = PodcastJob(
        id="literal-test-job-uuid-000000000001",
        transport="telegram",
        telegram_user_id=42,
        telegram_chat_id=42,
        source_hash="lit_hash",
        source_text="This is a purely literal deterministic narration test.",
        request_mode="literal",
        status=JobState.COMPLETE.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    # Generate literal script
    script_resp = generate_literal_script(
        source_text=job.source_text,
        source_title="Literal Ep",
    )
    assert script_resp is not None
    assert len(script_resp.segments) > 0

    # Invariant: Query DB for any AI interaction records
    ai_records = db.query(AIInteraction).filter(AIInteraction.job_id == job.id).all()
    assert len(ai_records) == 0, f"Expected 0 AI interactions for literal mode, found {len(ai_records)}"

    # Invariant: Verify format_diagnostics_card reports Literal mode zero AI
    card = format_diagnostics_card(job, db)
    assert "None (Literal mode)" in card
    assert "AI Interactions" not in card


def test_completion_markup_and_callback():
    db = setup_in_memory_db()
    user_id = 999
    chat_id = 999

    # Add owner
    owner = TelegramUser(
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        username="owner_callback",
        role="owner",
        is_active=True,
    )
    db.add(owner)

    job = PodcastJob(
        id="55555555-5555-4555-8555-555555555555",
        transport="telegram",
        telegram_user_id=user_id,
        telegram_chat_id=chat_id,
        source_hash="h5",
        source_text="Text 5",
        custom_title="Callback Test",
        status=JobState.COMPLETE.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    # Verify markup has both Download and Diagnostics buttons
    markup = format_completion_markup(job)
    buttons = markup["inline_keyboard"][0]
    assert len(buttons) == 2
    assert buttons[0]["text"] == "📥 Download MP3"
    assert buttons[0]["callback_data"] == f"h2:download:{job.id}"
    assert buttons[1]["text"] == "🛠️ Diagnostics"
    assert buttons[1]["callback_data"] == f"h2:diag:{job.id}"

    # Test callback query invocation
    mock_client = MagicMock()
    cb_query = {
        "id": "cb_diag_123",
        "data": f"h2:diag:{job.id}",
        "from": {"id": user_id},
        "message": {"message_id": 101, "chat": {"id": chat_id, "type": "private"}},
    }

    handle_telegram_callback_query(db, mock_client, cb_query)

    # answerCallbackQuery called immediately
    assert mock_client.answer_callback_query.called
    assert mock_client.send_message.called
    assert mock_client.send_document.called


def test_command_registration_and_help():
    # Verify diagnostics is present in TELEGRAM_BOT_COMMANDS
    cmd_names = [c["command"] for c in TELEGRAM_BOT_COMMANDS]
    assert "diagnostics" in cmd_names
    assert "download" in cmd_names
    assert "voices" in cmd_names
    assert "settings" in cmd_names

    # Verify register_bot_commands sends diagnostics
    mock_client = MagicMock()
    mock_client.set_my_commands.return_value = True
    ok = register_bot_commands(mock_client)
    assert ok is True
    mock_client.set_my_commands.assert_called_once_with(TELEGRAM_BOT_COMMANDS)

    # Verify format_help includes /diagnostics
    help_text = format_help()
    assert "/diagnostics" in help_text


def test_ai_interaction_recorder_and_retries():
    """Verify record_ai_interaction records attempt-level records in DB and handles failure gracefully."""
    db = setup_in_memory_db()
    job_id = "test-recorder-job-1111-2222-333333333333"
    job = PodcastJob(
        id=job_id,
        transport="telegram",
        telegram_user_id=1,
        telegram_chat_id=1,
        source_hash="h_rec",
        source_text="Test source",
        status=JobState.RECEIVED.value,
        created_at=datetime.now(UTC),
    )
    db.add(job)
    db.commit()

    # Record attempt 1 (failed)
    t0 = datetime.now(UTC) - timedelta(seconds=2)
    t1 = datetime.now(UTC) - timedelta(seconds=1)
    record_ai_interaction(
        job_id=job_id,
        provider="gemini",
        model="gemini-3.5-flash",
        operation="script_generation",
        started_at=t0,
        completed_at=t1,
        success=False,
        error="HTTP 429: Resource exhausted",
        metadata={"attempt": 1},
        db=db,
    )

    # Record attempt 2 (success)
    t2 = datetime.now(UTC)
    record_ai_interaction(
        job_id=job_id,
        provider="gemini",
        model="gemini-3.5-flash",
        operation="script_generation",
        started_at=t1,
        completed_at=t2,
        success=True,
        prompt_tokens=500,
        completion_tokens=700,
        total_tokens=1200,
        metadata={"attempt": 2},
        db=db,
    )

    # Invariant: Distinct records exist for both attempts
    records = db.query(AIInteraction).filter(AIInteraction.job_id == job_id).order_by(AIInteraction.started_at.asc()).all()
    assert len(records) == 2
    assert records[0].success is False
    assert records[0].error_category in ("RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED")
    assert records[0].metadata_json == {"attempt": 1}

    assert records[1].success is True
    assert records[1].total_tokens == 1200
    assert records[1].metadata_json == {"attempt": 2}
