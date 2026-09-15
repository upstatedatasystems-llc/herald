"""Unit tests for TTS Chunk Manager cache, hashing, and resume compatibility.

Tests:
1. Hash identity is based on effective spoken input and v2 prefix.
2. Valid existing chunk files with matching text hash are safely reused on restart.
3. Chunks with modified spoken text hash invalidate stale audio and reset to PENDING.
4. Completed valid chunks are not unnecessarily regenerated.
"""

from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base, PodcastJob, PodcastTTSChunk
from herald.tts.chunk_manager import compute_chunk_text_hash, sync_and_prepare_chunks
from herald.tts.chunker import TTSChunk


def create_test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_hash_identity_versioned_spoken():
    h1 = compute_chunk_text_hash("J W S T", "af_heart", 1.0)
    h2 = compute_chunk_text_hash("J W S T", "af_heart", 1.0)
    h_canonical = compute_chunk_text_hash("JWST", "af_heart", 1.0)
    h_voice = compute_chunk_text_hash("J W S T", "am_adam", 1.0)

    assert h1 == h2
    # Spoken representation differs from raw canonical representation
    assert h1 != h_canonical
    # Voice difference produces distinct hash
    assert h1 != h_voice


def test_sync_and_prepare_chunks_reuses_valid_matching_cache(tmp_path: Path):
    db = create_test_db()
    job_id = "test-job-cache-1"
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    job = PodcastJob(id=job_id, source_type="text", source_hash="test-hash", source_text="Test source text")
    db.add(job)
    db.commit()

    voice = "af_heart"
    speed = 1.0
    spoken_text = "This is a valid spoken chunk."
    text_hash = compute_chunk_text_hash(spoken_text, voice, speed)

    chunk_file = chunks_dir / "chunk_0001.wav"
    chunk_file.write_bytes(b"RIFF$ \x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00 \x00\x00" + b"\x00" * 100)

    existing_chunk = PodcastTTSChunk(
        job_id=job_id,
        chunk_index=1,
        text_hash=text_hash,
        status="COMPLETED",
        attempt_count=1,
        local_path=str(chunk_file),
    )
    db.add(existing_chunk)
    db.commit()

    script_chunks = [TTSChunk(index=1, text=spoken_text)]

    with patch("herald.tts.chunk_manager.validate_audio_file") as mock_val:
        mock_val.return_value = {"valid": True, "size_bytes": 100, "duration_seconds": 1.0}
        prepared = sync_and_prepare_chunks(
            db=db,
            job_id=job_id,
            script_chunks=script_chunks,
            voice=voice,
            speed=speed,
            chunks_dir=chunks_dir,
        )

    assert len(prepared) == 1
    # Chunk was reused as COMPLETED
    assert prepared[0].status == "COMPLETED"
    assert prepared[0].chunk_index == 1


def test_sync_and_prepare_chunks_invalidates_stale_hash(tmp_path: Path):
    db = create_test_db()
    job_id = "test-job-cache-2"
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    job = PodcastJob(id=job_id, source_type="text", source_hash="test-hash", source_text="Test source text")
    db.add(job)
    db.commit()

    voice = "af_heart"
    speed = 1.0
    old_hash = compute_chunk_text_hash("Old canonical text", voice, speed)

    chunk_file = chunks_dir / "chunk_0001.wav"
    chunk_file.write_bytes(b"dummy audio data")

    existing_chunk = PodcastTTSChunk(
        job_id=job_id,
        chunk_index=1,
        text_hash=old_hash,
        status="COMPLETED",
        attempt_count=1,
        local_path=str(chunk_file),
    )
    db.add(existing_chunk)
    db.commit()

    # Now the chunk has new spoken text
    new_spoken = "New normalized spoken text"
    script_chunks = [TTSChunk(index=1, text=new_spoken)]

    prepared = sync_and_prepare_chunks(
        db=db,
        job_id=job_id,
        script_chunks=script_chunks,
        voice=voice,
        speed=speed,
        chunks_dir=chunks_dir,
    )

    assert len(prepared) == 1
    # Stale chunk was invalidated to PENDING and stale file removed
    assert prepared[0].status == "PENDING"
    assert prepared[0].text_hash == compute_chunk_text_hash(new_spoken, voice, speed)
    assert not chunk_file.exists()
