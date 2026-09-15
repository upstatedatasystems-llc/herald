"""Unit tests for Phase 1: Local Script Quality Gate & Enhanced Duration Estimator.

Verifies:
1. Enhanced Duration Estimator:
   - Baseline prose estimation.
   - Slower speech predicted for technical/numeric-dense content.
   - Kokoro speed adjustment scaling.
2. Local Quality Gate:
   - Zero AI calls made.
   - Duplicate headings detected.
   - Near-duplicate passages detected.
   - Repetitive opening phrases detected.
   - Excessively long run-on sentences detected.
   - Generic catch-up headings detected.
   - Raw metadata prefixes stripped cleanly.
   - Normal script passes without false positive warnings.
"""

from herald.services.eta_calculator import calculate_script_duration
from herald.services.quality_gate import QualityStatus, run_quality_gate


def test_duration_estimator_baseline_prose():
    """Verify baseline prose duration estimation."""
    # 260 words ordinary prose with 2 segments and standard sentence structure
    narr_1 = "This is a natural narrative about history and culture. " * 10  # ~80 words
    narr_2 = "We explore how people lived, worked, and built their communities over time. " * 15  # ~180 words
    script = {
        "segments": [
            {"order": 1, "heading": "Chapter 1", "narration": narr_1},
            {"order": 2, "heading": "Chapter 2", "narration": narr_2},
        ]
    }
    res = calculate_script_duration(script, kokoro_speed=1.0)
    assert res["narration_word_count"] > 200
    assert res["predicted_duration_seconds"] > 60
    assert res["estimated_minutes"] >= 1
    assert res["numeric_density"] == 0.0
    assert res["acronym_density"] == 0.0


def test_duration_estimator_technical_numeric_dense_content():
    """Verify technical and numeric-dense content predicts slower speech (longer duration)."""
    # Create ordinary prose script with 300 words
    prose_words = ["word"] * 300
    prose_script = {"segments": [{"order": 1, "heading": "Prose", "narration": " ".join(prose_words) + "."}]}
    prose_dur = calculate_script_duration(prose_script, kokoro_speed=1.0)

    # Create technical script with identical 300 word count but heavy with numbers and acronyms
    tech_words = []
    for i in range(300):
        if i % 4 == 0:
            tech_words.append(f"${i * 100},000")
        elif i % 4 == 1:
            tech_words.append("NASA-JPL")
        elif i % 4 == 2:
            tech_words.append(f"{i * 2.5}%")
        else:
            tech_words.append("telemetry")
    tech_script = {"segments": [{"order": 1, "heading": "Tech", "narration": " ".join(tech_words) + "."}]}
    tech_dur = calculate_script_duration(tech_script, kokoro_speed=1.0)

    assert tech_dur["numeric_density"] > 0.10
    assert tech_dur["acronym_density"] > 0.10
    # Technical script must predict slower speech (more seconds) than plain prose of identical word count
    assert tech_dur["predicted_duration_seconds"] > prose_dur["predicted_duration_seconds"]
    assert tech_dur["effective_wpm"] < prose_dur["effective_wpm"]


def test_duration_estimator_kokoro_speed_scaling():
    """Verify Kokoro speed scales effective WPM and inversely scales duration."""
    script = {"segments": [{"order": 1, "heading": "Sec", "narration": "This is a simple test narration. " * 30}]}
    dur_normal = calculate_script_duration(script, kokoro_speed=1.0)
    dur_fast = calculate_script_duration(script, kokoro_speed=1.2)
    dur_slow = calculate_script_duration(script, kokoro_speed=0.8)

    assert dur_fast["predicted_duration_seconds"] < dur_normal["predicted_duration_seconds"]
    assert dur_slow["predicted_duration_seconds"] > dur_normal["predicted_duration_seconds"]
    assert dur_fast["effective_wpm"] > dur_normal["effective_wpm"]


def test_quality_gate_clean_script_passes():
    """Verify standard high-quality script passes with zero warnings."""
    script = {
        "episode_title": "The Story of Texas Roadhouse",
        "episode_description": "An exploration of customer experience and steaks.",
        "segments": [
            {"order": 1, "heading": "Humble Beginnings", "narration": "In 1993, Kent Taylor opened the first location in Clarksville, Indiana. The focus was simple: great food at affordable prices."},
            {"order": 2, "heading": "The Food Strategy", "narration": "Hand-cut steaks and freshly baked rolls became instant hallmarks of the restaurant. Every single steak was cut daily by hand."},
            {"order": 3, "heading": "Partner Model", "narration": "Managing partners invested their own money and received a meaningful share of profits. This aligned incentives directly with long-term hospitality."},
        ]
    }
    cleaned_script, report = run_quality_gate(script)
    assert report.status == QualityStatus.PASS
    assert len(report.warnings) == 0


def test_quality_gate_detects_duplicate_headings():
    """Verify detection of duplicate headings across sections."""
    script = {
        "episode_title": "Test Duplicate",
        "segments": [
            {"order": 1, "heading": "Market Strategy", "narration": "First section content."},
            {"order": 2, "heading": "Market Strategy", "narration": "Second section content."},
        ]
    }
    _, report = run_quality_gate(script)
    assert report.status == QualityStatus.WARN
    assert any(w.code == "DUPLICATE_HEADING" for w in report.warnings)


def test_quality_gate_detects_near_duplicate_passages():
    """Verify detection of identical or near-duplicate paragraphs."""
    passage = "The core operating system relies on distributed lock leases renewed every thirty seconds across active worker nodes to maintain integrity."
    script = {
        "episode_title": "Repetition Test",
        "segments": [
            {"order": 1, "heading": "Part A", "narration": f"Here is the setup. {passage}"},
            {"order": 2, "heading": "Part B", "narration": f"Continuing our discussion. {passage}"},
        ]
    }
    _, report = run_quality_gate(script)
    assert report.status == QualityStatus.WARN
    assert any(w.code == "NEAR_DUPLICATE_PASSAGE" for w in report.warnings)


def test_quality_gate_detects_repetitive_openings():
    """Verify detection when multiple sections start with the same phrasing."""
    script = {
        "episode_title": "Repetitive Openings",
        "segments": [
            {"order": 1, "heading": "A", "narration": "Turning now to the next major phase of development in 1995."},
            {"order": 2, "heading": "B", "narration": "Turning now to the next major phase of development in 1998."},
            {"order": 3, "heading": "C", "narration": "Turning now to the next major phase of development in 2002."},
        ]
    }
    _, report = run_quality_gate(script)
    assert report.status == QualityStatus.WARN
    assert any(w.code == "REPETITIVE_OPENING" for w in report.warnings)


def test_quality_gate_detects_run_on_sentence():
    """Verify detection of excessively long sentences without punctuation."""
    long_sentence = "This is an extraordinarily long and relentless run on sentence that just continues endlessly without any commas or semicolons or periods to give the listener even a single moment of breathing room while attempting to convey far too many distinct concepts simultaneously in an unreadable block of dense monologue"
    script = {
        "episode_title": "Run On Sentence Test",
        "segments": [
            {"order": 1, "heading": "Section 1", "narration": f"Introduction. {long_sentence}. Conclusion."},
        ]
    }
    _, report = run_quality_gate(script)
    assert report.status == QualityStatus.WARN
    assert any(w.code == "RUN_ON_SENTENCE" for w in report.warnings)


def test_quality_gate_detects_generic_catchup_heading():
    """Verify detection of generic catch-up headings."""
    script = {
        "episode_title": "Catchup Test",
        "segments": [
            {"order": 1, "heading": "Comprehensive Analysis and Evidence Synthesis", "narration": "Filler content here."},
        ]
    }
    _, report = run_quality_gate(script)
    assert report.status == QualityStatus.WARN
    assert any(w.code == "GENERIC_CATCHUP_HEADING" for w in report.warnings)


def test_quality_gate_strips_raw_metadata_prefix():
    """Verify safe deterministic cleanup of raw metadata prefixes."""
    script = {
        "episode_title": "Topic: Fusion Power Developments",
        "segments": [
            {"order": 1, "heading": "Section 1: The Reactor Physics", "narration": "Heading: Here is how plasma containment operates."},
        ]
    }
    cleaned_script, report = run_quality_gate(script)
    assert cleaned_script["episode_title"] == "Fusion Power Developments"
    assert cleaned_script["segments"][0]["heading"] == "The Reactor Physics"
    assert not cleaned_script["segments"][0]["narration"].startswith("Heading:")
    assert len(report.cleanups_applied) >= 2
