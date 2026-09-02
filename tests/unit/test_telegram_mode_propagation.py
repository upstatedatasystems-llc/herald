from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.config import settings
from herald.db.models import Base, PodcastJob
from herald.gemini.schema import PodcastScriptResponse, PodcastSegment
from herald.telegram.auth import generate_pairing_code, verify_and_claim_pairing_code
from herald.telegram.bot import process_telegram_update
from herald.telegram.client import TelegramClient


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


def test_telegram_mode_directives_propagate_to_created_jobs(db_session, monkeypatch):
    """
    Test Requirement 1:
    Verify that Telegram messages containing mode directives propagate correctly through
    process_telegram_update -> handle_telegram_content_message -> HeraldRequest -> PodcastJob:
    - literal -> request_mode == 'literal'
    - brief -> request_mode == 'brief'
    - standard -> request_mode == 'standard'
    - research low -> request_mode == 'research', research_depth == 'low'
    - research medium -> request_mode == 'research', research_depth == 'medium'
    - research high -> request_mode == 'research', research_depth == 'high'
    """
    # 1. Authorize owner
    code = generate_pairing_code(db_session)
    verify_and_claim_pairing_code(db_session, code, user_id=12345, chat_id=12345, username="owner")

    # 2. Configure Gemini provider in settings so AI modes are accepted
    monkeypatch.setattr(settings, "AI_PROVIDER", "gemini")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "fake-gemini-key")

    # 3. Mock AI generation boundaries so no external calls are made
    fake_script = PodcastScriptResponse(
        episode_title="Test Episode",
        episode_description="Test episode description",
        segments=[PodcastSegment(order=1, heading="Introduction", narration="Test segment narration.")],
        warnings=[],
    )
    fake_research_dossier = {
        "source_summary": "Summary",
        "verification": [],
        "useful_context": [],
        "outdated_or_uncertain": [],
        "research_sources": [],
    }

    monkeypatch.setattr(
        "herald.gemini.client.generate_podcast_script",
        lambda *args, **kwargs: fake_script,
    )
    monkeypatch.setattr(
        "herald.gemini.client.generate_grounded_research",
        lambda *args, **kwargs: {"raw_text": "evidence", "research_sources": []},
    )
    monkeypatch.setattr(
        "herald.gemini.client.normalize_research_dossier",
        lambda *args, **kwargs: MagicMock(model_dump=lambda: fake_research_dossier),
    )

    mock_client = MagicMock(spec=TelegramClient)

    test_cases = [
        ("literal\n\nDirect text for literal reading.", 101, "literal", None),
        ("brief\n\nDirect text for brief summary.", 102, "brief", None),
        ("standard\n\nDirect text for standard podcast.", 103, "standard", None),
        ("research low\n\nDirect text for research low.", 104, "research", "low"),
        ("research medium\n\nDirect text for research medium.", 105, "research", "medium"),
        ("research high\n\nDirect text for research high.", 106, "research", "high"),
    ]

    for text_payload, msg_id, expected_mode, expected_depth in test_cases:
        update = {
            "update_id": 1000 + msg_id,
            "message": {
                "message_id": msg_id,
                "from": {"id": 12345, "username": "owner"},
                "chat": {"id": 12345, "type": "private"},
                "text": text_payload,
            },
        }

        process_telegram_update(db_session, mock_client, update)

        job = db_session.query(PodcastJob).filter_by(telegram_message_id=msg_id).first()
        assert job is not None, f"Job was not created for message_id {msg_id}"
        assert job.request_mode == expected_mode, f"Expected request_mode '{expected_mode}', got '{job.request_mode}'"
        assert job.research_depth == expected_depth, f"Expected research_depth '{expected_depth}', got '{job.research_depth}'"
