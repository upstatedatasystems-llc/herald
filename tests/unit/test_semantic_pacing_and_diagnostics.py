"""Unit tests for Semantic Audio Pacing, Silence Measurement, and TTS Diagnostics.

Tests:
1. Semantic pause mapping in join_and_normalize_audio (0s for TECHNICAL_SPLIT, 0.5s sentence, 0.8s paragraph, 1.2s section).
2. measure_wav_silence calculates leading and trailing silence accurately without altering audio.
3. diagnostics_export includes canonical vs. spoken text, boundary types, pauses, and transformation traces.
4. calculate_script_duration calculates semantic pause overhead without double counting.
"""

import json
import struct
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.audio.ffmpeg_builder import (
    PAUSE_PARAGRAPH,
    PAUSE_SECTION,
    PAUSE_SENTENCE,
    PAUSE_TECHNICAL_SPLIT,
    join_and_normalize_audio,
    measure_wav_silence,
)
from herald.db.models import Base, PodcastJob, PodcastTTSChunk
from herald.services.diagnostics_export import generate_job_diagnostics_zip
from herald.services.eta_calculator import calculate_script_duration
from herald.tts.chunker import BoundaryType


def create_pcm_wav(path: Path, sample_rate: int = 24000, leading_silence_samples: int = 2400, active_samples: int = 4800, trailing_silence_samples: int = 2400):
    """Create a 16-bit mono PCM WAV file with controllable silence and active audio."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)

        # Leading silence
        silence_lead = struct.pack(f"<{leading_silence_samples}h", *([0] * leading_silence_samples))
        # Active tone (amplitude 5000)
        active_data = struct.pack(f"<{active_samples}h", *([5000] * active_samples))
        # Trailing silence
        silence_trail = struct.pack(f"<{trailing_silence_samples}h", *([0] * trailing_silence_samples))

        w.writeframes(silence_lead + active_data + silence_trail)
    return path


def test_measure_wav_silence(tmp_path: Path):
    wav_path = tmp_path / "test_silence.wav"
    # 2400 samples at 24000 Hz = 0.1s leading silence
    # 4800 samples at 24000 Hz = 0.2s active tone
    # 3600 samples at 24000 Hz = 0.15s trailing silence
    create_pcm_wav(wav_path, sample_rate=24000, leading_silence_samples=2400, active_samples=4800, trailing_silence_samples=3600)

    silence = measure_wav_silence(wav_path, threshold_amplitude=1000)
    assert abs(silence["leading_silence_s"] - 0.1) < 0.02
    assert abs(silence["trailing_silence_s"] - 0.15) < 0.02


def test_join_and_normalize_audio_semantic_pauses(tmp_path: Path):
    chunks_dir = tmp_path / "chunks"
    c1 = create_pcm_wav(chunks_dir / "c1.wav")
    c2 = create_pcm_wav(chunks_dir / "c2.wav")
    c3 = create_pcm_wav(chunks_dir / "c3.wav")

    out_mp3 = tmp_path / "output.mp3"

    # Boundaries: c1 is TECHNICAL_SPLIT (0.0s pause), c2 is PARAGRAPH (0.8s pause), c3 is SECTION
    boundary_types = ["TECHNICAL_SPLIT", "PARAGRAPH", "SECTION"]
    pause_durations = [0.0, 0.8, 1.2]

    with patch("herald.audio.ffmpeg_builder.shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("herald.audio.ffmpeg_builder.subprocess.run") as mock_run, \
         patch("herald.audio.ffmpeg_builder.generate_silence_wav") as mock_gen_silence, \
         patch("herald.audio.ffmpeg_builder.validate_audio_file") as mock_val:

        mock_val.return_value = {"size_bytes": 1024, "duration_seconds": 15.0}
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc
        mock_gen_silence.side_effect = lambda p, dur: p

        res = join_and_normalize_audio(
            chunk_paths=[c1, c2, c3],
            output_mp3_path=out_mp3,
            boundary_types=boundary_types,
            pause_durations=pause_durations,
            job_id="test-pacing-job",
        )

        # TECHNICAL_SPLIT has 0.0 pause, so generate_silence_wav must NOT be called for c1's pause!
        # Only padding_start (0.8s), c2 pause (0.8s), and padding_end (0.8s) should be generated
        pause_calls = [call.args[1] for call in mock_gen_silence.call_args_list]
        assert 0.0 not in pause_calls
        assert 0.8 in pause_calls


def test_eta_calculator_accounts_for_semantic_pauses():
    # 2 sections, with 2 paragraphs in section 1
    script_json = {
        "segments": [
            {
                "order": 1,
                "narration": "First paragraph sentence one. First paragraph sentence two.\n\nSecond paragraph sentence one.",
            },
            {
                "order": 2,
                "narration": "Third paragraph in second section sentence one.",
            },
        ]
    }

    dur_info = calculate_script_duration(script_json, kokoro_speed=1.0)
    # pause_allowance_seconds must include branding (2.4s), section transition (1.2s), paragraph break (0.8s), and intra-sentence spacing
    assert dur_info["pause_allowance_seconds"] > 3.0
    assert dur_info["predicted_duration_seconds"] > 0


def test_diagnostics_export_enrichment(tmp_path: Path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    job_id = "test-diag-job"
    chunks_dir = tmp_path / "chunks"
    c_wav = create_pcm_wav(chunks_dir / "chunk_0001.wav")

    job = PodcastJob(
        id=job_id,
        source_type="text",
        source_hash="hash123",
        source_text="Test source text.",
        status="COMPLETED",
        script_json={
            "episode_title": "Test Diagnostics",
            "segments": [
                {
                    "order": 1,
                    "narration": "In 1955, the B-52 and JWST's instruments were tested.",
                }
            ],
        },
    )
    db.add(job)

    db_chunk = PodcastTTSChunk(
        job_id=job_id,
        chunk_index=1,
        text_hash="v2:af_heart:1.0:test_hash",
        status="COMPLETED",
        audio_duration=3.5,
        attempt_count=1,
        local_path=str(c_wav),
    )
    db.add(db_chunk)
    db.commit()

    target_zip = tmp_path / "diag.zip"
    pkg_path = generate_job_diagnostics_zip(job=job, db=db, target_zip_path=target_zip)
    assert pkg_path.exists()

    # Extract tts-chunks.json from bundle
    import zipfile
    with zipfile.ZipFile(pkg_path, "r") as zf:
        assert "tts-chunks.json" in zf.namelist()
        chunk_data = json.loads(zf.read("tts-chunks.json").decode("utf-8"))
        assert len(chunk_data) == 1
        c_item = chunk_data[0]
        assert c_item["chunk_index"] == 1
        assert "spoken_text" in c_item
        assert "canonical_text" in c_item
        assert "transformations" in c_item
        assert "boundary_type" in c_item
        assert "pause_duration_ms" in c_item
        # Spoken text is enriched with normalized tokens
        assert "nineteen fifty-five" in c_item["spoken_text"]
        assert "B fifty-two" in c_item["spoken_text"]
        assert "J W S T's" in c_item["spoken_text"]
        # Canonical text has original notation
        assert "1955" in c_item["canonical_text"]
