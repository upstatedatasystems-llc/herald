"""Unit tests for Phase 2 Narration & Audio Quality final correction pass.

Covers:
A. Shared pause policy (chunker, ffmpeg_builder, and eta_calculator derive from same source)
B. Branding normalization (canonical remains unchanged, spoken text normalized, transformations recorded)
C. Test reel diagnostics (dry-run writes tts-chunks.json with intro/body/outro distinction)
D. Test reel fail-closed behavior (incomplete synthesis exits non-zero and refuses partial acceptance)
E. Year and numeric identifier normalization (natural contexts vs. guarded technical contexts)
F. Herald pronunciation A/B testing (conservative default vs. candidate override)
"""

import json
import math
import struct
import sys
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

from herald.audio.branding import (
    render_intro_narration,
    synthesize_branding_segment,
)
from herald.audio.ffmpeg_builder import measure_wav_silence
from herald.audio.pause_policy import (
    PAUSE_BRANDING,
    PAUSE_BY_BOUNDARY,
    PAUSE_DURATION_BY_BOUNDARY,
    PAUSE_PADDING_END,
    PAUSE_PADDING_START,
    PAUSE_PARAGRAPH,
    PAUSE_SECTION,
    PAUSE_SENTENCE,
    PAUSE_TECHNICAL_SPLIT,
    BoundaryType,
    get_pause_duration,
)
from herald.services.eta_calculator import calculate_script_duration
from herald.tts.chunker import (
    PAUSE_BY_BOUNDARY as CHUNKER_PAUSES,
)
from herald.tts.lexicon import DEFAULT_LEXICON, PronunciationLexicon
from herald.tts.normalizer import normalize_for_speech

project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from tools.tts_test_reel import run_test_reel  # noqa: E402


# ==============================================================================
# A. Shared Pause Policy
# ==============================================================================
def test_shared_pause_policy_identity():
    """Verify that chunker and ffmpeg_builder derive from the authoritative pause policy."""
    assert PAUSE_TECHNICAL_SPLIT == 0.0
    assert PAUSE_SENTENCE == 0.5
    assert PAUSE_PARAGRAPH == 0.8
    assert PAUSE_SECTION == 1.2
    assert PAUSE_BRANDING == 1.2
    assert PAUSE_PADDING_START == 0.8
    assert PAUSE_PADDING_END == 0.8

    # Verify chunker mapping identity
    for b_type in BoundaryType:
        assert CHUNKER_PAUSES[b_type] == PAUSE_BY_BOUNDARY[b_type]
        assert get_pause_duration(b_type) == PAUSE_BY_BOUNDARY[b_type]
        assert PAUSE_DURATION_BY_BOUNDARY[b_type.value] == PAUSE_BY_BOUNDARY[b_type]


def test_duration_estimator_derives_from_pause_policy():
    """Verify that duration prediction derives directly from the authoritative pause constants."""
    script_json = {
        "segments": [
            {
                "order": 1,
                "narration": "Paragraph one sentence one. Paragraph one sentence two.\n\nParagraph two sentence one.",
            },
            {
                "order": 2,
                "narration": "Section two sentence one. Section two sentence two.",
            },
        ]
    }

    dur_info = calculate_script_duration(script_json, kokoro_speed=1.0, include_branding=False)

    # 2 sections -> 2 * PAUSE_SECTION (2.4s)
    # 3 total paragraphs (2 in seg 1, 1 in seg 2) -> (3 - 2) * PAUSE_PARAGRAPH (0.8s)
    # 5 total sentences -> (5 - 3) * PAUSE_SENTENCE (1.0s)
    expected_pauses = (2 * PAUSE_SECTION) + (1 * PAUSE_PARAGRAPH) + (2 * PAUSE_SENTENCE)
    assert abs(dur_info["pause_allowance_seconds"] - expected_pauses) < 1e-4

    # When branding is included, adds PAUSE_BRANDING + PAUSE_PADDING_START + PAUSE_PADDING_END
    dur_branding = calculate_script_duration(script_json, kokoro_speed=1.0, include_branding=True)
    expected_branding_pauses = expected_pauses + PAUSE_BRANDING + PAUSE_PADDING_START + PAUSE_PADDING_END
    assert abs(dur_branding["pause_allowance_seconds"] - expected_branding_pauses) < 1e-4


# ==============================================================================
# B. Branding Normalization
# ==============================================================================
def test_branding_normalization_through_spoken_layer(tmp_path: Path):
    """Verify canonical branding remains written English while Kokoro receives spoken text."""
    canonical_intro = render_intro_narration(
        episode_title="Quantum Leap in 2026",
        publisher="Nature",
        target_minutes=2,
    )
    assert "Herald" in canonical_intro
    assert "2026" in canonical_intro

    mock_kokoro = MagicMock()
    wav_out = tmp_path / "test_branding_intro.wav"

    with patch("herald.audio.branding.validate_audio_file"), \
         patch("herald.audio.branding.inspect_pcm_wav_file", return_value={"duration_seconds": 3.5}):
        res = synthesize_branding_segment(
            text=canonical_intro,
            output_wav_path=wav_out,
            kokoro_client=mock_kokoro,
            voice="af_heart",
            speed=1.0,
            segment_name="intro branding",
        )

    # 1. Canonical text remains untouched
    assert res["canonical_text"] == canonical_intro
    # 2. Spoken text has 2026 normalized to words
    assert "twenty twenty-six" in res["spoken_text"]
    # 3. Text actually sent to Kokoro was spoken text
    mock_kokoro.synthesize_chunk.assert_called_once()
    sent_text = mock_kokoro.synthesize_chunk.call_args[1]["text"]
    assert sent_text == res["spoken_text"]
    assert "twenty twenty-six" in sent_text
    # 4. Companion metadata written with versioned hash
    meta_file = wav_out.with_suffix(".meta.json")
    assert meta_file.exists()
    meta_data = json.loads(meta_file.read_text(encoding="utf-8"))
    assert len(meta_data["text_hash"]) == 64
    assert meta_data["canonical_text"] == canonical_intro
    assert meta_data["spoken_text"] == res["spoken_text"]


# ==============================================================================
# C. Test Reel Diagnostics
# ==============================================================================
def test_test_reel_dry_run_diagnostics(tmp_path: Path):
    """Verify dry-run writes tts-chunks.json with intro/body/outro and canonical vs spoken text."""
    code = run_test_reel(output_dir=tmp_path, dry_run=True, voice="af_heart", speed=1.0)
    assert code == 0

    chunks_file = tmp_path / "tts-chunks.json"
    summary_file = tmp_path / "test-reel-summary.json"
    assert chunks_file.exists()
    assert summary_file.exists()

    chunks = json.loads(chunks_file.read_text(encoding="utf-8"))
    assert len(chunks) >= 4  # Intro + body chunks + Outro

    # Intro verification
    intro = chunks[0]
    assert intro["index"] == 0
    assert intro["segment_type"] == "INTRO"
    assert intro["boundary_type"] == "BRANDING"
    assert intro["pause_duration_ms"] == int(round(PAUSE_BRANDING * 1000))
    assert intro["status"] == "DRY_RUN"
    assert "Herald presents:" in intro["canonical_text"]

    # Body chunk verification
    body = chunks[1]
    assert body["segment_type"] == "BODY"
    assert body["canonical_text"] != body["spoken_text"]

    # Outro verification
    outro = chunks[-1]
    assert outro["segment_type"] == "OUTRO"
    assert outro["boundary_type"] == "BRANDING"
    assert outro["pause_duration_ms"] == 0
    assert "listening to Herald" in outro["canonical_text"]

    # Summary verification
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    assert summary["status"] == "DRY_RUN"
    assert summary["total_chunks"] == len(chunks)


# ==============================================================================
# D. Test Reel Fail-Closed Behavior
# ==============================================================================
def test_test_reel_fails_closed_on_chunk_error(tmp_path: Path):
    """Verify that a failure in any chunk causes a non-zero exit and no acceptance MP3."""
    with patch("tools.tts_test_reel.KokoroClient") as mock_client_cls:
        mock_instance = mock_client_cls.return_value
        # Fail on the second body chunk
        def side_effect(text, output_path, **kwargs):
            if "GPT-4o" in text or "twenty twenty-six" in text:
                raise RuntimeError("Kokoro server 503 Service Unavailable")
            output_path.write_bytes(b"RIFF....WAVEfmt ....data....")

        mock_instance.synthesize_chunk.side_effect = side_effect

        with patch("tools.tts_test_reel.validate_audio_file"), \
             patch("tools.tts_test_reel.inspect_pcm_wav_file", return_value={"duration_seconds": 2.0}), \
             patch("tools.tts_test_reel.measure_wav_silence", return_value={"leading_silence_s": 0.05, "trailing_silence_s": 0.05}), \
             patch("tools.tts_test_reel.synthesize_branding_segment", return_value={"duration_seconds": 2.5}):

            exit_code = run_test_reel(output_dir=tmp_path, dry_run=False, voice="af_heart", speed=1.0)

    # Must exit with non-zero code
    assert exit_code == 1

    # Final acceptance MP3 must NOT exist
    acceptance_mp3 = tmp_path / "herald_phase2_test_reel_af_heart.mp3"
    assert not acceptance_mp3.exists()

    # Diagnostics file must record the failure
    chunks_file = tmp_path / "tts-chunks.json"
    assert chunks_file.exists()
    chunks = json.loads(chunks_file.read_text(encoding="utf-8"))
    failed_entries = [c for c in chunks if c["status"] == "FAILED"]
    assert len(failed_entries) > 0
    assert "503" in failed_entries[0]["error_detail"]

    # Summary must report FAILED status
    summary_file = tmp_path / "test-reel-summary.json"
    assert summary_file.exists()
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    assert summary["status"] == "FAILED"
    assert len(summary["failed_chunks"]) > 0


# ==============================================================================
# E. Year and Numeric Identifier Normalization
# ==============================================================================
def test_year_natural_contexts_normalization():
    """Verify natural language year contexts normalize correctly."""
    cases = [
        ("In 1955, the program started.", "In nineteen fifty-five, the program started."),
        ("By 2026, the fleet expanded.", "By twenty twenty-six, the fleet expanded."),
        ("Since 1998, they collaborated.", "Since nineteen ninety-eight, they collaborated."),
        ("The year 2001 was a turning point.", "The year two thousand one was a turning point."),
        ("1955 marked the start.", "nineteen fifty-five marked the start."),
        ("2026 was a major milestone.", "twenty twenty-six was a major milestone."),
        ("from 1955 to 1960", "from nineteen fifty-five to nineteen sixty"),
    ]
    for raw, expected in cases:
        res = normalize_for_speech(raw)
        assert res.spoken_text == expected, f"Failed for '{raw}': got '{res.spoken_text}' expected '{expected}'"


def test_year_guarded_technical_contexts():
    """Verify technical, version, model, port, RFC, and IP contexts avoid false year conversion."""
    guarded_cases = [
        "version 2026",
        "model 2026",
        "port 2026",
        "RFC 2026",
        "192.168.1.1",
        "serial ABC-2026",
        "model X2026",
    ]
    for case in guarded_cases:
        res = normalize_for_speech(case)
        assert "twenty twenty-six" not in res.spoken_text
        assert "nineteen" not in res.spoken_text


# ==============================================================================
# F. Herald Pronunciation A/B Testing
# ==============================================================================
def test_herald_default_remains_conservative():
    """Verify production default for 'Herald' remains conservative 'Herald'."""
    assert DEFAULT_LEXICON["Herald"] == "Herald"
    res = normalize_for_speech("This is Herald.")
    assert res.spoken_text == "This is Herald."
    assert res.canonical_text == "This is Herald."


def test_herald_candidate_override_changes_spoken_only():
    """Verify candidate override alters spoken output without modifying canonical text."""
    candidate_lexicon = PronunciationLexicon(overrides={"Herald": "HAIR-uld"})
    res = normalize_for_speech("This is Herald reporting.", lexicon=candidate_lexicon)

    assert res.canonical_text == "This is Herald reporting."
    assert res.spoken_text == "This is HAIR-uld reporting."
    assert len(res.transformations) == 1
    assert res.transformations[0].original == "Herald"
    assert res.transformations[0].spoken == "HAIR-uld"


# ==============================================================================
# G. Phase 2 Closeout Verifications (Packaging & Silence Measurement)
# ==============================================================================
def test_test_reel_packaged_in_worker_dockerfile():
    """Verify tools directory is copied into the worker Docker image."""
    root_dir = Path(__file__).parent.parent.parent
    dockerfile = root_dir / "docker" / "Dockerfile.worker"
    assert dockerfile.exists()
    content = dockerfile.read_text(encoding="utf-8")
    assert "COPY tools/ ./tools/" in content


def test_synthetic_wav_silence_measurement(tmp_path: Path):
    """Verify synthetic WAV with known silence regions measures approximately expected durations."""
    wav_path = tmp_path / "synthetic_silence.wav"
    rate = 24000
    # 150 ms leading silence (3600 samples)
    # 1000 ms active audio (24000 samples, 440 Hz tone, amplitude 6000)
    # 350 ms trailing silence (8400 samples, low background dither amplitude 150)
    samples: list[int] = [0] * 3600
    for i in range(24000):
        samples.append(int(6000 * math.sin(2 * math.pi * 440 * i / rate)))
    for i in range(8400):
        samples.append(int(150 * math.sin(i)))

    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    res = measure_wav_silence(wav_path)
    assert res["leading_silence_s"] is not None
    assert res["trailing_silence_s"] is not None
    assert abs(res["leading_silence_s"] - 0.150) < 0.02
    assert abs(res["trailing_silence_s"] - 0.350) < 0.02


def test_zero_silence_wav_measurement(tmp_path: Path):
    """Verify active audio from sample 0 to end reports zero silence."""
    wav_path = tmp_path / "zero_silence.wav"
    rate = 24000
    samples = [int(6000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(12000)]

    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    res = measure_wav_silence(wav_path)
    assert res["leading_silence_s"] == 0.0
    assert res["trailing_silence_s"] == 0.0


def test_silence_measurement_failure_reports_none(tmp_path: Path):
    """Verify measurement failure reports None rather than falsely reporting 0.0."""
    non_existent = tmp_path / "missing.wav"
    res = measure_wav_silence(non_existent)
    assert res["leading_silence_s"] is None
    assert res["trailing_silence_s"] is None

    empty_wav = tmp_path / "empty.wav"
    empty_wav.write_bytes(b"")
    res_empty = measure_wav_silence(empty_wav)
    assert res_empty["leading_silence_s"] is None
    assert res_empty["trailing_silence_s"] is None


def test_semantic_pause_policy_remains_unchanged():
    """Verify pause durations remain identical to approved Phase 2 values."""
    assert PAUSE_TECHNICAL_SPLIT == 0.0
    assert PAUSE_SENTENCE == 0.5
    assert PAUSE_PARAGRAPH == 0.8
    assert PAUSE_SECTION == 1.2
    assert PAUSE_BRANDING == 1.2
    assert PAUSE_PADDING_START == 0.8
    assert PAUSE_PADDING_END == 0.8
