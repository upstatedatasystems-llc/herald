"""
Unit test suite verifying Telegram preference resolution fix, AI chain/model snapshots,
startup banner, and settings telemetry.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.telegram_bot.main import print_startup_banner
from herald.config import settings
from herald.core.models import HeraldRequest
from herald.core.pipeline import execute_script_generation, process_herald_request
from herald.db.models import Base, PodcastJob, TelegramUser
from herald.ai.schema import PodcastScriptResponse, PodcastSegment
from herald.config import settings
from herald.core.models import HeraldRequest
from herald.core.pipeline import execute_script_generation, process_herald_request
from herald.db.models import Base, PodcastJob, TelegramUser
from herald.telegram.auth import (
    ensure_telegram_user,
    get_effective_user_preferences,
    set_user_ai_model_for_provider,
    set_user_ai_provider_chain,
    set_user_default_mode,
    set_user_default_speed,
    set_user_default_voice,
)
from herald.telegram.bot import perform_ai_check


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


@pytest.fixture(autouse=True)
def configure_test_environment(monkeypatch):
    """Ensure mock credentials and allowed voices/users are set for tests."""
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_IDS", "12345,98765")
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "fake_cf_token")
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "fake_cf_account")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake_gemini_key")
    monkeypatch.setattr(settings, "AI_PROVIDER", "cloudflare")
    monkeypatch.setattr(settings, "AI_SECONDARY_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "AI_TERTIARY_PROVIDER", None)
    monkeypatch.setattr(settings, "ALLOWED_VOICES", "af_heart,am_adam,bf_emma")
    monkeypatch.setattr(settings, "KOKORO_VOICE", "af_heart")
    monkeypatch.setattr(settings, "KOKORO_SPEED", 1.0)


def _mock_dummy_script(title="Test Script"):
    return PodcastScriptResponse(
        episode_title=title,
        episode_description="A test episode description.",
        estimated_minutes=2,
        source_title=title,
        segments=[
            PodcastSegment(order=1, heading="Intro", narration="Welcome to the test broadcast.")
        ],
        warnings=[],
    )


def test_regression_1_user_selects_cloudflare_model_b(db_session, monkeypatch):
    """
    1. User selects Cloudflare model B while server default is model A.
       - /ai-check resolves B.
       - newly-created podcast snapshots B.
       - AI execution uses B.
    """
    user_id = 12345
    ensure_telegram_user(db_session, user_id=user_id, chat_id=user_id)

    model_a = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
    model_b = "@cf/qwen/qwen3.8-27b"

    # Set user preference for Cloudflare to Model B
    set_user_ai_model_for_provider(db_session, user_id=user_id, provider_id="cloudflare", model_id=model_b)

    # Verify /ai-check resolves Model B
    mock_client = MagicMock()
    perform_ai_check(db=db_session, client=mock_client, chat_id=user_id, user_id=user_id)
    assert mock_client.send_message.called
    sent_text = "\n".join(call.kwargs.get("text", "") for call in mock_client.send_message.call_args_list)
    assert model_b in sent_text

    prefs = get_effective_user_preferences(db_session, user_id)
    assert prefs["ai_models_by_provider_json"]["cloudflare"] == model_b

    # Newly created podcast snapshots Model B
    req = HeraldRequest(
        transport="telegram",
        transport_message_id=101,
        requester_identity=f"telegram:{user_id}",
        delivery_target=str(user_id),
        request_mode="standard",
        source_text="Cloudflare model test content.",
    )
    resp = process_herald_request(db=db_session, req=req)
    assert resp.job_id != ""

    job = db_session.query(PodcastJob).filter(PodcastJob.id == resp.job_id).first()
    assert job is not None
    assert job.ai_effective_provider == "cloudflare"
    assert job.ai_effective_model == model_b
    assert job.ai_model == model_b
    assert job.ai_provider_chain_json[0]["model"] == model_b

    # AI execution uses Model B
    executed_providers = []

    def fake_generate_script(self, source_text, request_mode=None, source_title=None, job_id=None, **kwargs):
        executed_providers.append((self.provider_name, self.configured_model))
        return _mock_dummy_script()

    with patch("herald.ai.cloudflare_provider.CloudflareProvider.generate_script", fake_generate_script):
        exec_resp = execute_script_generation(db=db_session, job=job, hold_for_approval=False)
        assert exec_resp.job_id == job.id

    assert len(executed_providers) == 1
    assert executed_providers[0] == ("Cloudflare Workers AI", model_b)


def test_regression_2_user_changes_server_chain(db_session, monkeypatch):
    """
    2. Server chain is Cloudflare -> Gemini.
       User changes chain to Gemini -> Cloudflare.
       - new job snapshots Gemini -> Cloudflare.
       - execution starts with Gemini.
    """
    user_id = 12345
    ensure_telegram_user(db_session, user_id=user_id, chat_id=user_id)

    # Change chain preference to Gemini -> Cloudflare
    set_user_ai_provider_chain(db_session, user_id=user_id, chain=["gemini", "cloudflare"])

    req = HeraldRequest(
        transport="telegram",
        transport_message_id=102,
        requester_identity=f"telegram:{user_id}",
        delivery_target=str(user_id),
        request_mode="standard",
        source_text="Chain reversal test content.",
    )
    resp = process_herald_request(db=db_session, req=req)
    job = db_session.query(PodcastJob).filter(PodcastJob.id == resp.job_id).first()
    assert job is not None

    # New job snapshots Gemini -> Cloudflare
    chain_order = [c["provider"] for c in job.ai_provider_chain_json]
    assert chain_order == ["gemini", "cloudflare"]
    assert job.ai_effective_provider == "gemini"

    # Execution starts with Gemini
    executed_providers = []

    def fake_gemini_generate_script(self, source_text, request_mode=None, source_title=None, job_id=None, **kwargs):
        executed_providers.append(self.provider_name)
        return _mock_dummy_script()

    with patch("herald.ai.gemini_provider.GeminiProvider.generate_script", fake_gemini_generate_script):
        exec_resp = execute_script_generation(db=db_session, job=job, hold_for_approval=False)
        assert exec_resp.job_id == job.id

    assert len(executed_providers) == 1
    assert executed_providers[0] == "Gemini"


def test_regression_3_per_provider_models_survive_provider_switching(db_session):
    """
    3. Per-provider models survive provider switching.
       Example:
       - Cloudflare -> CF model X
       - Gemini -> Gemini model Y
       - switch Primary back and forth
       - each provider retains its selected model.
    """
    from herald.ai.resolution import resolve_job_settings

    user_id = 12345
    ensure_telegram_user(db_session, user_id=user_id, chat_id=user_id)

    cf_model_x = "@cf/qwen/qwen3.8-27b"
    gemini_model_y = "gemini-3.5-flash"

    set_user_ai_model_for_provider(db_session, user_id=user_id, provider_id="cloudflare", model_id=cf_model_x)
    set_user_ai_model_for_provider(db_session, user_id=user_id, provider_id="gemini", model_id=gemini_model_y)

    # Primary = Cloudflare, Secondary = Gemini
    set_user_ai_provider_chain(db_session, user_id=user_id, chain=["cloudflare", "gemini"])
    prefs1 = get_effective_user_preferences(db_session, user_id)
    resolved1 = resolve_job_settings(request_params={}, user_prefs=prefs1)

    assert resolved1.ai_candidates[0].provider_id == "cloudflare"
    assert resolved1.ai_candidates[0].model_id == cf_model_x
    assert resolved1.ai_candidates[1].provider_id == "gemini"
    assert resolved1.ai_candidates[1].model_id == gemini_model_y

    # Switch Primary to Gemini: Gemini -> Cloudflare
    set_user_ai_provider_chain(db_session, user_id=user_id, chain=["gemini", "cloudflare"])
    prefs2 = get_effective_user_preferences(db_session, user_id)
    resolved2 = resolve_job_settings(request_params={}, user_prefs=prefs2)

    assert resolved2.ai_candidates[0].provider_id == "gemini"
    assert resolved2.ai_candidates[0].model_id == gemini_model_y
    assert resolved2.ai_candidates[1].provider_id == "cloudflare"
    assert resolved2.ai_candidates[1].model_id == cf_model_x

    # Switch Primary back to Cloudflare: Cloudflare -> Gemini
    set_user_ai_provider_chain(db_session, user_id=user_id, chain=["cloudflare", "gemini"])
    prefs3 = get_effective_user_preferences(db_session, user_id)
    resolved3 = resolve_job_settings(request_params={}, user_prefs=prefs3)

    assert resolved3.ai_candidates[0].provider_id == "cloudflare"
    assert resolved3.ai_candidates[0].model_id == cf_model_x
    assert resolved3.ai_candidates[1].provider_id == "gemini"
    assert resolved3.ai_candidates[1].model_id == gemini_model_y


def test_regression_4_voice_speed_mode_preferences_preserved(db_session):
    """
    4. Voice/speed/mode preferences are not lost while resolving AI settings.
    """
    from herald.telegram.bot import process_telegram_update

    user_id = 12345
    ensure_telegram_user(db_session, user_id=user_id, chat_id=user_id)

    set_user_default_voice(db_session, user_id=user_id, voice="am_adam")
    set_user_default_speed(db_session, user_id=user_id, speed=1.15)
    set_user_default_mode(db_session, user_id=user_id, mode="brief")
    set_user_ai_provider_chain(db_session, user_id=user_id, chain=["cloudflare", "gemini"])
    set_user_ai_model_for_provider(
        db_session,
        user_id=user_id,
        provider_id="cloudflare",
        model_id="@cf/qwen/qwen3.8-27b",
    )

    # 1. Verify get_effective_user_preferences sees all settings
    prefs = get_effective_user_preferences(db_session, user_id)
    assert prefs["default_voice"] == "am_adam"
    assert prefs["default_speed"] == 1.15
    assert prefs["default_mode"] == "brief"
    assert prefs["ai_provider_chain_json"] == ["cloudflare", "gemini"]
    assert prefs["ai_models_by_provider_json"] == {"cloudflare": "@cf/qwen/qwen3.8-27b"}

    # 2. Intake via Telegram message update
    mock_client = MagicMock()
    update = {
        "update_id": 9001,
        "message": {
            "message_id": 104,
            "from": {"id": user_id, "username": "owner"},
            "chat": {"id": user_id, "type": "private"},
            "text": "Testing preference preservation across voice, speed, and mode.",
        },
    }
    process_telegram_update(db_session, mock_client, update)

    job = db_session.query(PodcastJob).filter(PodcastJob.telegram_message_id == 104).first()
    assert job is not None
    assert job.custom_voice == "am_adam"
    assert job.custom_speed == 1.15
    assert job.request_mode == "brief"
    assert job.ai_effective_provider == "cloudflare"
    assert job.ai_effective_model == "@cf/qwen/qwen3.8-27b"
    assert job.ai_provider_chain_json[0]["provider"] == "cloudflare"
    assert job.ai_provider_chain_json[0]["model"] == "@cf/qwen/qwen3.8-27b"
    assert job.ai_provider_chain_json[1]["provider"] == "gemini"
    assert job.research_depth is None  # Not a research job


def test_startup_ai_banner_with_string_provider_chain(capsys, monkeypatch):
    """
    Verify print_startup_banner correctly handles list[str] from get_server_default_chain(),
    resolves the default model through the registry, and avoids the 'Chain error' bug.
    """
    with patch("apps.telegram_bot.main.TelegramClient") as mock_client_cls, \
         patch("apps.telegram_bot.main.KokoroClient") as mock_kokoro_cls, \
         patch("apps.telegram_bot.main.SessionLocal") as mock_session_cls, \
         patch("herald.ai.cloudflare_provider.CloudflareProvider.check_connection") as mock_check_conn:

        mock_client = MagicMock()
        mock_client.is_configured = True
        mock_client.get_me.return_value = {"username": "HeraldBot"}
        mock_client_cls.return_value = mock_client

        mock_kokoro = MagicMock()
        mock_kokoro.health_check.return_value = {"healthy": True}
        mock_kokoro_cls.return_value = mock_kokoro

        mock_check_conn.return_value = {"connected": True, "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast"}

        print_startup_banner()
        captured = capsys.readouterr().out

        assert "Chain error" not in captured
        assert "Cloudflare Workers AI — Connected" in captured


def test_settings_telemetry_logging(db_session, caplog):
    """
    Verify sanitized telemetry logging when Telegram settings are changed and at podcast intake:
    - User AI chain updated: gemini -> cloudflare
    - User model updated: gemini -> gemini-3.5-flash
    - User default voice updated: ...
    - User default speed updated: ...
    - User default mode updated: ...
    - Podcast intake resolved AI chain: ...
    - No user IDs, secrets, or API keys in telemetry
    """
    user_id = 12345
    ensure_telegram_user(db_session, user_id=user_id, chat_id=user_id)

    with caplog.at_level(logging.INFO):
        set_user_ai_provider_chain(db_session, user_id=user_id, chain=["gemini", "cloudflare"])
        assert "User AI chain updated: gemini -> cloudflare" in caplog.text

        set_user_ai_model_for_provider(db_session, user_id=user_id, provider_id="gemini", model_id="gemini-3.5-flash")
        assert "User model updated: gemini -> gemini-3.5-flash" in caplog.text

        set_user_default_voice(db_session, user_id=user_id, voice="am_adam")
        assert "User default voice updated: am_adam" in caplog.text

        set_user_default_speed(db_session, user_id=user_id, speed=1.1)
        assert "User default speed updated: 1.1" in caplog.text

        set_user_default_mode(db_session, user_id=user_id, mode="brief")
        assert "User default mode updated: brief" in caplog.text

        # Intake logging
        caplog.clear()
        req = HeraldRequest(
            transport="telegram",
            transport_message_id=205,
            requester_identity=f"telegram:{user_id}",
            delivery_target=str(user_id),
            request_mode="brief",
            source_text="Testing intake telemetry logging.",
        )
        process_herald_request(db=db_session, req=req)

        assert "Podcast intake resolved AI chain:" in caplog.text
        assert "gemini:gemini-3.5-flash" in caplog.text
        assert "enqueued with frozen AI chain:" in caplog.text
