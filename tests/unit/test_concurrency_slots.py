import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from herald.concurrency import (
    get_effective_tts_global_slots,
    get_semaphores,
    get_tts_slot_wait_timeout_seconds,
    initialize_semaphores,
    reset_semaphores_for_tests,
    resolve_concurrency_settings,
    tts_slot_lock,
)
from herald.config import settings
from herald.db.models import Base, PodcastJob, PodcastTTSChunk
from herald.tts.chunk_manager import TTSChunk, synthesize_single_chunk
from herald.tts.kokoro_client import KokoroClient


@pytest.fixture
def db_session_factory(tmp_path):
    db_file = tmp_path / "test_concurrency.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"timeout": 30.0, "check_same_thread": False},
    )
    with engine.connect() as conn:
        conn.execute(sa_text("PRAGMA journal_mode=WAL;"))
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    return TestingSession


def test_concurrency_profiles_resolution():
    """
    Test profile resolution using exact supported profile names and cpus_override:
    - 'single' -> 1 global slot
    - 'balanced' -> scales with CPUs
    - 'auto' -> scales with CPUs
    """
    # Single profile always returns 1 slot
    cfg_single_1 = resolve_concurrency_settings(profile="single", cpus_override=1)
    assert cfg_single_1.tts_global_slots == 1

    cfg_single_8 = resolve_concurrency_settings(profile="single", cpus_override=8)
    assert cfg_single_8.tts_global_slots == 1

    # Balanced / Auto profile with controlled cpus_override
    cfg_bal_1 = resolve_concurrency_settings(profile="balanced", cpus_override=1)
    assert cfg_bal_1.tts_global_slots == 1

    cfg_bal_2 = resolve_concurrency_settings(profile="balanced", cpus_override=2)
    assert cfg_bal_2.tts_global_slots == 2

    cfg_bal_4 = resolve_concurrency_settings(profile="balanced", cpus_override=4)
    assert cfg_bal_4.tts_global_slots == 3

    cfg_bal_8 = resolve_concurrency_settings(profile="balanced", cpus_override=8)
    assert cfg_bal_8.tts_global_slots == 6

    # Auto profile behaves identically to balanced
    cfg_auto_1 = resolve_concurrency_settings(profile="auto", cpus_override=1)
    assert cfg_auto_1.tts_global_slots == 1

    cfg_auto_2 = resolve_concurrency_settings(profile="auto", cpus_override=2)
    assert cfg_auto_2.tts_global_slots == 2

    cfg_auto_4 = resolve_concurrency_settings(profile="auto", cpus_override=4)
    assert cfg_auto_4.tts_global_slots == 3


def test_slot_count_resolution_explicit_override(monkeypatch):
    """Explicit HERALD_TTS_GLOBAL_SLOTS override is honored."""
    monkeypatch.setattr(settings, "HERALD_CONCURRENCY_PROFILE", "auto")
    monkeypatch.setattr(settings, "HERALD_TTS_GLOBAL_SLOTS", 5)

    slots = get_effective_tts_global_slots()
    assert slots == 5


def test_kokoro_synthesis_timeout_derivation(monkeypatch):
    """
    Test that changing KOKORO_SYNTHESIS_TIMEOUT_SECONDS alters get_tts_slot_wait_timeout_seconds()
    and derives the intended 1.5x headroom (minimum 180s).
    """
    # 1. Default (180s) -> slot wait timeout = 270.0s (180 * 1.5)
    monkeypatch.setattr(settings, "KOKORO_SYNTHESIS_TIMEOUT_SECONDS", 180)
    assert get_tts_slot_wait_timeout_seconds() == 270.0

    # 2. Configured higher (200s) -> Kokoro synth timeout = 200, slot wait timeout = 300.0s (200 * 1.5)
    monkeypatch.setattr(settings, "KOKORO_SYNTHESIS_TIMEOUT_SECONDS", 200)
    assert settings.KOKORO_SYNTHESIS_TIMEOUT_SECONDS == 200
    assert get_tts_slot_wait_timeout_seconds() == 300.0

    # 3. Configured lower (60s) -> bounded by minimum floor of 180.0s
    monkeypatch.setattr(settings, "KOKORO_SYNTHESIS_TIMEOUT_SECONDS", 60)
    assert get_tts_slot_wait_timeout_seconds() == 180.0

    with tts_slot_lock(db=None) as slot:
        assert slot is None


def test_local_sqlite_single_slot_no_deadlock(db_session_factory, tmp_path, monkeypatch):
    """
    Regression test: When global slots = 1 under SQLite/non-PostgreSQL,
    synthesize_single_chunk does not double-acquire or deadlock on itself.
    """
    reset_semaphores_for_tests()
    monkeypatch.setattr(settings, "HERALD_TTS_GLOBAL_SLOTS", 1)
    monkeypatch.setattr(settings, "HERALD_CONCURRENCY_PROFILE", "single")
    initialize_semaphores(settings.get_concurrency_config())

    db = db_session_factory()
    job = PodcastJob(
        id="test-job-single-slot-1",
        transport="telegram",
        source_hash="h1",
        source_text="Sample text for single chunk",
    )
    db.add(job)
    db_chunk = PodcastTTSChunk(
        job_id=job.id,
        chunk_index=1,
        text_hash="th1",
        status="PENDING",
    )
    db.add(db_chunk)
    db.commit()
    db.close()

    mock_kokoro = MagicMock(spec=KokoroClient)

    def mock_synth(text, output_path, voice=None, speed=None, timeout=None):
        Path(output_path).write_bytes(
            b"RIFF\xa4\x0c\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x80\x0c\x00\x00"
            + (b"\x00" * 3200)
        )

    mock_kokoro.synthesize_chunk.side_effect = mock_synth

    sems = get_semaphores()
    chunk_item = TTSChunk(index=1, text="Sample text for single chunk")

    # Must complete cleanly without timing out or deadlocking
    t0 = time.monotonic()
    out = synthesize_single_chunk(
        session_factory=db_session_factory,
        job_id="test-job-single-slot-1",
        chunk=chunk_item,
        voice="af_heart",
        speed=1.0,
        synthesis_timeout=5.0,
        chunks_dir=tmp_path,
        kokoro_client=mock_kokoro,
        global_semaphore=sems.global_tts,
        per_job_semaphore=sems.create_per_job_tts_semaphore(),
        worker_id="w-test-1",
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0
    assert out.exists()
    reset_semaphores_for_tests()


def test_local_sqlite_two_workers_two_slots_concurrency_bounded(
    db_session_factory, tmp_path, monkeypatch
):
    """
    Regression test: When global slots = 2 with two workers,
    multiple concurrent chunks execute in parallel without deadlocks,
    and maximum concurrent Kokoro sections never exceeds configured slots (2).
    """
    reset_semaphores_for_tests()
    monkeypatch.setattr(settings, "HERALD_TTS_GLOBAL_SLOTS", 2)
    monkeypatch.setattr(settings, "HERALD_CONCURRENCY_PROFILE", "auto")
    initialize_semaphores(settings.get_concurrency_config())

    db = db_session_factory()
    job = PodcastJob(
        id="test-job-multi-slot-2",
        transport="telegram",
        source_hash="h2",
        source_text="Sample text for two chunks",
    )
    db.add(job)
    for idx in (1, 2, 3, 4):
        c = PodcastTTSChunk(
            job_id=job.id,
            chunk_index=idx,
            text_hash=f"th{idx}",
            status="PENDING",
        )
        db.add(c)
    db.commit()
    db.close()

    active_in_kokoro = 0
    max_active_in_kokoro = 0
    lock = threading.Lock()

    def mock_synth(text, output_path, voice=None, speed=None, timeout=None):
        nonlocal active_in_kokoro, max_active_in_kokoro
        with lock:
            active_in_kokoro += 1
            if active_in_kokoro > max_active_in_kokoro:
                max_active_in_kokoro = active_in_kokoro
        time.sleep(0.08)
        with lock:
            active_in_kokoro -= 1
        Path(output_path).write_bytes(
            b"RIFF\xa4\x0c\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x80\x0c\x00\x00"
            + (b"\x00" * 3200)
        )

    mock_kokoro = MagicMock(spec=KokoroClient)
    mock_kokoro.synthesize_chunk.side_effect = mock_synth

    sems = get_semaphores()
    per_job_sem = sems.create_per_job_tts_semaphore()

    def run_worker(idx):
        chunk_item = TTSChunk(index=idx, text=f"Sample text chunk {idx}")
        synthesize_single_chunk(
            session_factory=db_session_factory,
            job_id="test-job-multi-slot-2",
            chunk=chunk_item,
            voice="af_heart",
            speed=1.0,
            synthesis_timeout=10.0,
            chunks_dir=tmp_path,
            kokoro_client=mock_kokoro,
            global_semaphore=sems.global_tts,
            per_job_semaphore=per_job_sem,
            worker_id=f"w-{idx}",
        )

    threads = [threading.Thread(target=run_worker, args=(i,)) for i in (1, 2, 3, 4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    # Concurrency must never exceed configured global slots (2)
    assert max_active_in_kokoro <= 2
    assert active_in_kokoro == 0
    reset_semaphores_for_tests()
