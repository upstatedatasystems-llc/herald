from herald.config import Settings
from herald.gemini.schema import PodcastScriptResponse
from herald.literal.script_generator import (
    extract_title_and_body,
    generate_literal_script,
    normalize_tts_spoken_text,
)


def test_normalize_tts_spoken_text():
    raw = "Here is **bold** and *italic* with a [link](https://example.com/test) and `code`.\n- Bullet point\n1. Numbered item"
    clean = normalize_tts_spoken_text(raw)
    assert "**" not in clean
    assert "*" not in clean
    assert "`" not in clean
    assert "[" not in clean
    assert "https://example.com" not in clean
    assert "link" in clean
    assert "Bullet point" in clean
    assert "Numbered item" in clean


def test_extract_title_and_body():
    # 1. Custom title
    t1, b1 = extract_title_and_body("First line\nSecond line", custom_title="Custom Ep")
    assert t1 == "Custom Ep"
    assert b1 == "First line\nSecond line"

    # 2. Leading markdown heading
    t2, b2 = extract_title_and_body("# Quantum Computing Breakthrough\n\nScientists announced...")
    assert t2 == "Quantum Computing Breakthrough"
    assert "Scientists announced..." in b2

    # 3. Explicit Title: prefix
    t3, b3 = extract_title_and_body("Title: Modern Robotics\n\nRobotics has evolved...")
    assert t3 == "Modern Robotics"
    assert "Robotics has evolved..." in b3


def test_literal_mode_generates_valid_script_without_ai(monkeypatch):
    """Point 8: Literal mode performs no AI call."""
    # Ensure any attempt to call Gemini will explode
    def mock_gemini_call(*args, **kwargs):
        raise RuntimeError("AI was called during literal mode!")

    monkeypatch.setattr("herald.gemini.client.generate_podcast_script", mock_gemini_call)

    sample_text = """
# Advances in Deep Learning

Deep learning has revolutionized artificial intelligence over the past decade.
Neural networks with billions of parameters now achieve state-of-the-art results across vision and language tasks.

## Scaling Laws and Efficiency

Recent research focuses heavily on compute-optimal scaling laws and dataset curation.
Engineers have demonstrated that training models on higher-quality filtered text yields superior capabilities.

## Conclusion

Future architectures are anticipated to combine reasoning techniques with multimodal perception.
"""

    script: PodcastScriptResponse = generate_literal_script(sample_text, source_title="AI Research")
    assert script.episode_title == "AI Research"
    assert len(script.segments) >= 2
    assert script.segments[0].order == 1
    assert script.segments[0].heading
    assert script.segments[0].narration
    assert script.estimated_minutes is not None and script.estimated_minutes >= 1
    assert "Literal deterministic mode" in script.warnings[0]


def test_herald_starts_with_no_ai_key():
    """Point 9 & 10: Herald starts with no AI key and defaults to Literal."""
    cfg = Settings(
        GEMINI_API_KEY="",
        AI_PROVIDER="none",
        TELEGRAM_BOT_TOKEN="mock-token",
        EMAIL_ALLOWED_SENDERS="",
        GOOGLE_DRIVE_FOLDER_ID="",
        HERALD_ENV="development",
    )
    assert not cfg.is_ai_configured()
    assert cfg.get_default_mode() == "literal"
