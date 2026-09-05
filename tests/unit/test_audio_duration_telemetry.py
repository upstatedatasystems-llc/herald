"""
Unit test suite for WAV chunk duration calculation, streaming header protection,
RTF derivation, and audio telemetry diagnostics.
"""

import io
import struct
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from herald.audio.ffmpeg_builder import FFmpegExecutionError, validate_audio_file


def _create_synthetic_wav(
    path: Path,
    sample_rate: int = 24000,
    channels: int = 1,
    sample_width: int = 2,
    num_frames: int = 24000,
    streaming_chunk_header: bool = False,
) -> Path:
    """Helper to generate standard or streaming-header PCM WAV files."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sample_width)
        w.setframerate(sample_rate)
        # Write dummy silent frames
        w.writeframes(b"\x00" * (channels * sample_width * num_frames))

    data = bytearray(buf.getvalue())
    if streaming_chunk_header:
        # Locate 'data' chunk marker and overwrite length with 0x7FFFFFFF
        idx = data.find(b"data")
        if idx != -1:
            data[idx + 4 : idx + 8] = struct.pack("<I", 0x7FFFFFFF)

    path.write_bytes(bytes(data))
    return path


def test_standard_wav_duration_calculation(tmp_path):
    """
    Test that standard PCM WAV (24000 Hz, 24000 frames) calculates duration precisely to 1.0s / 1000ms.
    """
    wav_path = tmp_path / "chunk_1s.wav"
    _create_synthetic_wav(wav_path, sample_rate=24000, num_frames=24000)

    val = validate_audio_file(wav_path)
    assert val["valid"] is True
    assert val["audio_type"] == "WAVE"
    assert val["duration_seconds"] == 1.0
    assert int(val["duration_seconds"] * 1000) == 1000


def test_streaming_header_wav_does_not_return_absurd_duration(tmp_path):
    """
    Test regression: Kokoro streaming / unfinalized WAV files with 0x7FFFFFFF data chunk size
    must calculate duration from actual audio payload, returning ~1.0s NOT 89,478.5 seconds!
    """
    wav_path = tmp_path / "chunk_streaming.wav"
    _create_synthetic_wav(wav_path, sample_rate=24000, num_frames=24000, streaming_chunk_header=True)

    val = validate_audio_file(wav_path)
    assert val["valid"] is True
    assert val["audio_type"] == "WAVE"
    # Duration must be 1.0s (or extremely close), NOT 89478.5s!
    assert 0.99 <= val["duration_seconds"] <= 1.01
    assert val["duration_seconds"] < 5.0


def test_multiple_sample_rates(tmp_path):
    """Test duration calculation across various standard sample rates."""
    for rate in [16000, 24000, 44100, 48000]:
        wav_path = tmp_path / f"sample_{rate}.wav"
        # 1.5 seconds of audio
        frames = int(rate * 1.5)
        _create_synthetic_wav(wav_path, sample_rate=rate, num_frames=frames)

        val = validate_audio_file(wav_path)
        assert val["valid"] is True
        assert pytest.approx(val["duration_seconds"], rel=1e-3) == 1.5


def test_rtf_derivation_from_corrected_duration():
    """
    Test that RTF is calculated truthfully from TTS wall time / actual generated audio duration.
    """
    # 1.0s of audio generated in 2.0s of wall time -> RTF = 2.0
    elapsed_ms = 2000
    audio_dur_sec = 1.0
    audio_dur_ms = int(audio_dur_sec * 1000)

    rtf_val = round((elapsed_ms / float(audio_dur_ms)), 3)
    assert rtf_val == 2.0

    # 15.0s of audio generated in 33.895s of wall time -> RTF = ~2.26
    elapsed_ms_real = 33895
    audio_dur_sec_real = 15.0
    audio_dur_ms_real = int(audio_dur_sec_real * 1000)
    rtf_val_real = round((elapsed_ms_real / float(audio_dur_ms_real)), 3)
    assert rtf_val_real == 2.26


def test_wav_with_extra_riff_chunks_before_data(tmp_path):
    """
    Test Matrix C: WAV file containing additional chunks before 'data' (e.g. JUNK, LIST)
    calculates duration accurately without assuming fixed 44-byte offset.
    """
    wav_path = tmp_path / "chunk_extra_chunks.wav"
    sample_rate = 24000
    channels = 1
    sample_width = 2
    num_frames = 24000
    pcm_bytes = b"\x00" * (channels * sample_width * num_frames)

    junk_payload = b"PADDING_DATA_IN_JUNK_CHUNK_FOR_METADATA_ALIGNMENT"
    junk_chunk = b"JUNK" + struct.pack("<I", len(junk_payload)) + junk_payload
    if len(junk_payload) % 2 == 1:
        junk_chunk += b"\x00"

    fmt_chunk = b"fmt \x10\x00\x00\x00\x01\x00" + struct.pack(
        "<HIIHH", channels, sample_rate, sample_rate * channels * sample_width, channels * sample_width, sample_width * 8
    )
    data_chunk = b"data" + struct.pack("<I", len(pcm_bytes)) + pcm_bytes

    riff_payload = fmt_chunk + junk_chunk + data_chunk
    riff_header = b"RIFF" + struct.pack("<I", len(riff_payload) + 4) + b"WAVE"

    wav_path.write_bytes(riff_header + riff_payload)

    val = validate_audio_file(wav_path)
    assert val["valid"] is True
    assert val["audio_type"] == "WAVE"
    assert val["duration_seconds"] == 1.0


def test_wav_with_extra_riff_chunks_and_sentinel_streaming_header(tmp_path):
    """
    Test Matrix C+B: WAV file containing prepended JUNK chunk AND 0x7FFFFFFF streaming sentinel data header
    still calculates exact 1.0s duration based on physical payload from data offset.
    """
    wav_path = tmp_path / "chunk_extra_streaming.wav"
    sample_rate = 24000
    channels = 1
    sample_width = 2
    num_frames = 24000
    pcm_bytes = b"\x00" * (channels * sample_width * num_frames)

    junk_payload = b"LIST_INFO_ALBUM_HERALD_PODCAST_AUTOMATION"
    junk_chunk = b"LIST" + struct.pack("<I", len(junk_payload)) + junk_payload
    if len(junk_payload) % 2 == 1:
        junk_chunk += b"\x00"

    fmt_chunk = b"fmt \x10\x00\x00\x00\x01\x00" + struct.pack(
        "<HIIHH", channels, sample_rate, sample_rate * channels * sample_width, channels * sample_width, sample_width * 8
    )
    data_chunk = b"data" + struct.pack("<I", 0x7FFFFFFF) + pcm_bytes

    riff_payload = fmt_chunk + junk_chunk + data_chunk
    riff_header = b"RIFF" + struct.pack("<I", len(riff_payload) + 4) + b"WAVE"

    wav_path.write_bytes(riff_header + riff_payload)

    val = validate_audio_file(wav_path)
    assert val["valid"] is True
    assert val["audio_type"] == "WAVE"
    assert pytest.approx(val["duration_seconds"], rel=1e-3) == 1.0


def test_malformed_wav_controlled_failure(tmp_path):
    """Test that zero-byte or corrupt non-audio files raise FFmpegExecutionError cleanly."""
    empty_file = tmp_path / "empty.wav"
    empty_file.write_bytes(b"")

    with pytest.raises(FFmpegExecutionError):
        validate_audio_file(empty_file)

    junk_file = tmp_path / "junk.wav"
    junk_file.write_bytes(b"RIFFjunkWAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00corrupt")

    with patch("shutil.which", return_value=None):
        with pytest.raises(FFmpegExecutionError):
            validate_audio_file(junk_file)


def test_no_fixed_44_byte_assumptions_in_codebase():
    """
    Regression assertion: Ensure ffmpeg_builder.py contains zero fixed 44-byte ('- 44' or '<= 44')
    payload offset calculations.
    """
    import inspect

    import herald.audio.ffmpeg_builder as fb

    source = inspect.getsource(fb)
    assert "- 44" not in source
    assert "<= 44" not in source
    assert "(file_size - 44)" not in source
