import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
import subprocess

import pytest

from herald.audio.ffmpeg_builder import (
    FFmpegExecutionError,
    join_and_normalize_audio,
)
from herald.concurrency import (
    get_semaphores,
    initialize_semaphores,
    reset_semaphores_for_tests,
)
from herald.config import settings


@pytest.fixture(autouse=True)
def reset_semaphores_fixture(monkeypatch):
    monkeypatch.setattr(settings, "HERALD_FFMPEG_CONCURRENCY", 1)
    reset_semaphores_for_tests()
    initialize_semaphores(settings.get_concurrency_config())
    yield
    reset_semaphores_for_tests()


def test_ffmpeg_concurrency_one_no_self_deadlock(tmp_path):
    """
    Prove that with HERALD_FFMPEG_CONCURRENCY=1, calling join_and_normalize_audio
    does not self-deadlock (since callers no longer wrap it in semaphores.ffmpeg).
    """
    chunk = tmp_path / "chunk_001.wav"
    chunk.write_bytes(b"RIFF....WAVEfmt ....data....")
    out_mp3 = tmp_path / "output.mp3"

    def mock_subprocess_run(cmd, *args, **kwargs):
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00HERALD_TEST_AUDIO_DATA_1234567890")
        mock_res = MagicMock()
        mock_res.returncode = 0
        return mock_res

    mock_val = {
        "valid": True,
        "size_bytes": 100,
        "duration_seconds": 5.0,
        "audio_type": "MP3",
    }

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("subprocess.run", side_effect=mock_subprocess_run) as mock_run, \
         patch("herald.audio.ffmpeg_builder.validate_audio_file", return_value=mock_val), \
         patch("herald.audio.ffmpeg_builder.embed_id3_metadata"):

        res = join_and_normalize_audio(
            chunk_paths=[chunk],
            output_mp3_path=out_mp3,
            episode_title="Test Episode",
            job_id="job-deadlock-test",
            insert_pauses=False,
        )

        assert res["duration_seconds"] == 5.0
        assert mock_run.called
        assert get_semaphores().ffmpeg._value == 1


def test_join_and_normalize_audio_reaches_subprocess(tmp_path):
    """
    Prove that join_and_normalize_audio actually reaches the subprocess.run call.
    """
    chunk = tmp_path / "chunk_001.wav"
    chunk.write_bytes(b"RIFF....WAVEfmt ....data....")
    out_mp3 = tmp_path / "output.mp3"

    def mock_subprocess_run(cmd, *args, **kwargs):
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00HERALD_TEST_AUDIO_DATA_1234567890")
        mock_res = MagicMock()
        mock_res.returncode = 0
        return mock_res

    mock_val = {
        "valid": True,
        "size_bytes": 200,
        "duration_seconds": 10.0,
        "audio_type": "MP3",
    }

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("subprocess.run", side_effect=mock_subprocess_run) as mock_run, \
         patch("herald.audio.ffmpeg_builder.validate_audio_file", return_value=mock_val), \
         patch("herald.audio.ffmpeg_builder.embed_id3_metadata"):

        res = join_and_normalize_audio(
            chunk_paths=[chunk],
            output_mp3_path=out_mp3,
            episode_title="Test Subprocess",
            job_id="job-subproc-test",
            insert_pauses=False,
        )

        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ffmpeg"
        assert str(out_mp3) in cmd


def test_simultaneous_encodes_obey_concurrency_limit(tmp_path):
    """
    Prove two simultaneous encodes strictly obey HERALD_FFMPEG_CONCURRENCY=1 limit.
    """
    chunk1 = tmp_path / "chunk1.wav"
    chunk1.write_bytes(b"RIFF....WAVEfmt ....data....")
    chunk2 = tmp_path / "chunk2.wav"
    chunk2.write_bytes(b"RIFF....WAVEfmt ....data....")

    active_count = 0
    max_active = 0
    count_lock = threading.Lock()

    mock_val = {
        "valid": True,
        "size_bytes": 100,
        "duration_seconds": 5.0,
        "audio_type": "MP3",
    }

    def mock_subprocess_run(cmd, *args, **kwargs):
        nonlocal active_count, max_active
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00HERALD_TEST_AUDIO_DATA_1234567890")
        with count_lock:
            active_count += 1
            if active_count > max_active:
                max_active = active_count
        time.sleep(0.05)
        with count_lock:
            active_count -= 1
        m = MagicMock()
        m.returncode = 0
        return m

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("subprocess.run", side_effect=mock_subprocess_run), \
         patch("herald.audio.ffmpeg_builder.validate_audio_file", return_value=mock_val), \
         patch("herald.audio.ffmpeg_builder.embed_id3_metadata"):

        t1 = threading.Thread(
            target=join_and_normalize_audio,
            kwargs={
                "chunk_paths": [chunk1],
                "output_mp3_path": tmp_path / "out1.mp3",
                "job_id": "job1",
                "insert_pauses": False,
            },
        )
        t2 = threading.Thread(
            target=join_and_normalize_audio,
            kwargs={
                "chunk_paths": [chunk2],
                "output_mp3_path": tmp_path / "out2.mp3",
                "job_id": "job2",
                "insert_pauses": False,
            },
        )

        t1.start()
        t2.start()
        t1.join()
        t2.join()

    assert max_active == 1


def test_ffmpeg_timeout_raises_error_cleans_output_releases_semaphore(tmp_path):
    """
    Prove that an FFmpeg timeout:
    1. Raises FFmpegExecutionError
    2. Removes partial output file
    3. Releases the semaphore permit
    """
    chunk = tmp_path / "chunk_001.wav"
    chunk.write_bytes(b"RIFF....WAVEfmt ....data....")
    out_mp3 = tmp_path / "partial_output.mp3"
    out_mp3.write_bytes(b"PARTIAL_FFMPEG_DATA")

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=300)), \
         patch("herald.audio.ffmpeg_builder.validate_audio_file"):

        with pytest.raises(FFmpegExecutionError, match="timed out"):
            join_and_normalize_audio(
                chunk_paths=[chunk],
                output_mp3_path=out_mp3,
                job_id="job-timeout-test",
                insert_pauses=False,
            )

    assert not out_mp3.exists()
    assert get_semaphores().ffmpeg._value == 1


def test_ffmpeg_failure_does_not_leak_semaphore(tmp_path):
    """
    Prove that an FFmpeg failure (exit code != 0) does not leak a semaphore permit.
    """
    chunk = tmp_path / "chunk_001.wav"
    chunk.write_bytes(b"RIFF....WAVEfmt ....data....")
    out_mp3 = tmp_path / "failed_output.mp3"
    out_mp3.write_bytes(b"FAILED_DATA")

    mock_res = MagicMock()
    mock_res.returncode = 1
    mock_res.stderr = "Conversion failed"

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("subprocess.run", return_value=mock_res), \
         patch("herald.audio.ffmpeg_builder.validate_audio_file"):

        with pytest.raises(FFmpegExecutionError, match="FFmpeg failed with exit code 1"):
            join_and_normalize_audio(
                chunk_paths=[chunk],
                output_mp3_path=out_mp3,
                job_id="job-fail-test",
                insert_pauses=False,
            )

    assert not out_mp3.exists()
    assert get_semaphores().ffmpeg._value == 1
