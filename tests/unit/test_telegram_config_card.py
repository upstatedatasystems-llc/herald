import pytest
from herald.config import settings
from herald.db.models import JobState, PodcastJob
from herald.telegram.formatters import (
    format_content_mode_menu,
    format_length_menu,
    format_podcast_config_card,
    format_research_depth_menu,
    format_settings,
)


def test_format_podcast_config_card_modes_and_limits():
    """Verify config card rendering, mode descriptions, and 64-byte callback limit."""
    job = PodcastJob(
        id="12345678-1234-5678-1234-567812345678",
        custom_title="Understanding Deep Neural Networks",
        source_url="https://example.com/deep-learning",
        source_text="This is a detailed article about deep learning and neural network architectures with extensive analysis.",
        content_mode="source",
        target_minutes="auto",
        research_depth="medium",
        status=JobState.AWAITING_CONFIGURATION.value,
    )

    text, markup = format_podcast_config_card(job)
    assert "Configure Your Podcast" in text
    assert "Understanding Deep Neural Networks" in text
    assert "Mode:" in text
    assert "Source" in text
    assert "Target Length:" in text
    assert "Auto" in text
    assert "Research Depth:" in text
    # In source mode, research depth is not applicable
    assert "N/A (Not used in Source)" in text

    # Verify callback data lengths <= 64 bytes
    keyboard = markup["inline_keyboard"]
    for row in keyboard:
        for btn in row:
            cb_data = btn["callback_data"]
            assert len(cb_data.encode("utf-8")) <= 64, f"Callback data too long: {cb_data}"

    # Verify buttons present
    flat_data = [btn["callback_data"] for row in keyboard for btn in row]
    assert f"h4:c:{job.id}:m:source" in flat_data
    assert f"h4:c:{job.id}:m:expanded" in flat_data
    assert f"h4:c:{job.id}:m:topic" in flat_data
    assert f"h4:c:{job.id}:m:literal" in flat_data
    assert f"h4:c:{job.id}:len:10" in flat_data
    assert f"h4:c:{job.id}:len:30" in flat_data
    assert f"h4:c:{job.id}:btn:def" in flat_data
    assert f"h4:c:{job.id}:btn:lit" in flat_data
    assert f"h4:c:{job.id}:btn:start" in flat_data
    assert f"h4:c:{job.id}:btn:cancel" in flat_data


def test_format_podcast_config_card_expanded_and_topic():
    """Verify expanded and topic mode enable research depth controls."""
    job_expanded = PodcastJob(
        id="12345678-1234-5678-1234-567812345678",
        custom_title="Quantum Computing Advances",
        source_text="Short seed on quantum computing.",
        content_mode="expanded",
        target_minutes="30",
        research_depth="high",
        status=JobState.AWAITING_CONFIGURATION.value,
    )

    text, markup = format_podcast_config_card(job_expanded)
    assert "Expanded" in text
    assert "30 min (~3,900 words)" in text
    assert "High" in text

    flat_data = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert f"h4:c:{job_expanded.id}:rd:none" not in flat_data
    assert f"h4:c:{job_expanded.id}:rd:low" in flat_data
    assert f"h4:c:{job_expanded.id}:rd:medium" in flat_data
    assert f"h4:c:{job_expanded.id}:rd:high" in flat_data

    # Check Literal mode
    job_literal = PodcastJob(
        id="12345678-1234-5678-1234-567812345678",
        custom_title="Literal Narration",
        source_text="Read verbatim please.",
        content_mode="literal",
        target_minutes="auto",
        research_depth=None,
        status=JobState.AWAITING_CONFIGURATION.value,
    )
    text_lit, markup_lit = format_podcast_config_card(job_literal)
    assert "Literal" in text_lit
    assert "N/A (Not used in Literal)" in text_lit


def test_default_settings_submenus():
    """Verify settings submenus render correctly and have valid callback data."""
    prefs = {
        "default_content_mode": "expanded",
        "default_target_minutes": "20",
        "default_research_depth": "high",
    }

    # Content Mode Menu
    m_text, m_markup = format_content_mode_menu(prefs)
    assert "Default Content Mode" in m_text
    assert "Expanded" in m_text
    for row in m_markup["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64

    # Length Menu
    l_text, l_markup = format_length_menu(prefs)
    assert "Default Target Length" in l_text
    assert "20" in l_text
    for row in l_markup["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64

    # Research Depth Menu
    r_text, r_markup = format_research_depth_menu(prefs)
    assert "Default Research Depth" in r_text
    assert "High" in r_text
    r_callbacks = [btn["callback_data"] for row in r_markup["inline_keyboard"] for btn in row]
    assert "h4:s:set_rd:none" not in r_callbacks
    assert "h4:s:set_rd:low" in r_callbacks
    assert "h4:s:set_rd:medium" in r_callbacks
    assert "h4:s:set_rd:high" in r_callbacks
    for row in r_markup["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64

    # Full Settings Menu displays defaults
    s_text, s_markup = format_settings(prefs)
    assert "Default Content Mode:" in s_text
    assert "Expanded" in s_text
    assert "Default Target Length:" in s_text
    assert "20" in s_text
    assert "Default Research Depth:" in s_text
    assert "High" in s_text

    s_callbacks = [btn["callback_data"] for row in s_markup["inline_keyboard"] for btn in row]
    assert "h4:s:mode" in s_callbacks
    assert "h4:s:length" in s_callbacks
    assert "h4:s:research" in s_callbacks
    assert "h3:settings:mode" not in s_callbacks
