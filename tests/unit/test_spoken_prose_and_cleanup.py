from herald.db.models import RequestMode
from herald.extraction.email_parser import (
    process_email_message,
)
from herald.extraction.source_cleaner import clean_source_text
from herald.gemini.client import load_system_prompt
from herald.services.eta_calculator import calculate_script_duration


def test_shared_source_cleaner_formatting_junk_removal():
    raw_text = """
    Skip to main content
    Menu | Subscribe | Sign In
    Printable Version
    
    # Major Breakthrough in Quantum Computing
    
    Researchers achieved a coherence time of [latex]t_c = 0.00505783\text{ seconds}[/latex].
    
    ![Quantum Computer Chip](https://example.com/images/chip.jpg?utm_source=newsletter&utm_medium=email)
    
    The experiment produced 2,000 pounds per square inch of pressure.
    
    Cookie Policy | Privacy Policy | All rights reserved.
    """
    cleaned = clean_source_text(raw_text)

    assert "Skip to main content" not in cleaned
    assert "Printable Version" not in cleaned
    assert "Cookie Policy" not in cleaned
    assert "[latex]" not in cleaned
    assert "t_c = 0.00505783" in cleaned
    assert "Major Breakthrough in Quantum Computing" in cleaned
    assert "2,000 pounds per square inch" in cleaned


def test_source_cleaner_regression_preserves_substantive_content():
    article = """
    ## Key Technical Findings
    - System throughput reached 14,500 operations per second.
    - Water-to-cement ratio (W/C) was maintained at 0.42.
    - Failure occurred at 450 degrees Celsius.
    """
    cleaned = clean_source_text(article)
    assert "14,500 operations per second" in cleaned
    assert "W/C" in cleaned
    assert "450 degrees Celsius" in cleaned


def test_research_depth_precedence_rules():
    # Explicit subject depth wins over body directive
    parsed1 = process_email_message(
        subject="Podcast: Research High",
        body_text="Research: Low\n\nArticle content about energy systems...",
    )
    assert parsed1.mode == RequestMode.RESEARCH
    assert parsed1.research_depth == "high"
    assert len(parsed1.warnings) == 1
    assert "overrode body directive" in parsed1.warnings[0]

    # Generic subject uses body directive
    parsed2 = process_email_message(
        subject="Podcast: Research",
        body_text="Research: Low\n\nArticle content about energy systems...",
    )
    assert parsed2.mode == RequestMode.RESEARCH
    assert parsed2.research_depth == "low"

    # Default depth is medium when unstated
    parsed3 = process_email_message(
        subject="Podcast: Research",
        body_text="Article content about energy systems...",
    )
    assert parsed3.mode == RequestMode.RESEARCH
    assert parsed3.research_depth == "medium"

    # Warning issued if Research directive passed on Brief or Standard
    parsed4 = process_email_message(
        subject="Podcast: Standard",
        body_text="Research: High\n\nArticle content...",
    )
    assert parsed4.mode == RequestMode.STANDARD
    assert parsed4.research_depth is None
    assert len(parsed4.warnings) == 1
    assert "ignored for non-Research mode" in parsed4.warnings[0]


def test_programmatic_duration_estimation():
    script_data = {
        "episode_title": "Test Title",
        "episode_description": "Test Description",
        "segments": [
            {
                "order": 1,
                "heading": "Intro",
                "narration": " ".join(["word"] * 150),  # Exactly 150 words
            },
            {
                "order": 2,
                "heading": "Body",
                "narration": " ".join(["word"] * 150),  # 150 words -> Total 300 words
            },
        ],
        "warnings": [],
    }

    # At WPM 136 baseline, 300 words + 2 segments (3s pause allowance) = 135 seconds
    dur = calculate_script_duration(script_data, kokoro_speed=1.0)
    assert dur["narration_word_count"] == 300
    assert dur["predicted_duration_seconds"] == 135
    assert dur["estimated_minutes"] == 2

    # At speed 1.2, 300 words / (136 * 1.2 / 60) + 3s = 113 seconds
    dur_fast = calculate_script_duration(script_data, kokoro_speed=1.2)
    assert dur_fast["predicted_duration_seconds"] == 113


def test_system_prompt_includes_spoken_prose_rules():
    prompt = load_system_prompt()
    assert "Spoken Prose Rules" in prompt
    assert "Have you ever wondered" in prompt
    assert "numbers" in prompt.lower()
    assert "tables" in prompt.lower()
