from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import Settings
from herald.core.models import HeraldRequest
from herald.core.pipeline import process_herald_request
from herald.db.models import Base, JobState, PodcastJob


def test_clean_telegram_install_environment(tmp_path, monkeypatch):
    """
    Smoke test: verify that a completely clean Telegram+Literal environment
    (with zero Gmail, Drive, Google OAuth, or n8n variables)
    renders configuration and executes jobs successfully.
    """
    clean_env = {
        "TELEGRAM_BOT_TOKEN": "123456789:ABCDefGhIjKlMnOpQrStUvWxYz",
        "AI_PROVIDER": "none",
        "DEFAULT_MODE": "literal",
        "HERALD_WORK_DIR": str(tmp_path / "work"),
        # Zero Gmail / Drive / n8n variables
    }

    test_settings = Settings(**clean_env)
    assert test_settings.is_production_valid() is True
    assert test_settings.is_ai_configured() is False
    assert test_settings.get_default_mode() == "literal"

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    # Process a Telegram request
    req = HeraldRequest(
        transport="telegram",
        transport_message_id="101",
        requester_identity="telegram:55555",
        delivery_target="55555",
        mode="literal",
        source_text="Clean install validation text for Herald podcast generation.",
        custom_title="Clean Install Episode",
    )

    with TestingSession() as db:
        res = process_herald_request(db, req)
        assert res.job_id is not None
        assert res.request_mode == "literal"
        assert res.status in (JobState.QUEUED_TTS.value, JobState.SCRIPT_READY.value, JobState.SCRIPTING.value, JobState.RECEIVED.value)

        job = db.query(PodcastJob).filter_by(id=res.job_id).first()
        assert job is not None
        assert job.transport == "telegram"
        assert job.telegram_chat_id == 55555
        assert job.script_json is not None
        assert len(job.script_json.get("segments", [])) > 0
