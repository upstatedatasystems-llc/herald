from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.db.models import Base, JobProcessingMetric, JobState, PodcastJob
from herald.services.eta_calculator import calculate_job_eta
from herald.services.performance_metrics import record_stage_metric


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_eta_v2_preserves_full_synthesis_duration_on_cache_reuse_retry(db_session):
    """
    Test that when a downstream stage fails and triggers a retry where chunk files are reused,
    the deterministic upsert of TTS_TOTAL preserves the original full-synthesis wall time
    and does not contaminate historical RTF with near-zero retry passes.
    """
    job_id = "retry-eta-job-1111-2222-333333333333"
    t0 = datetime.now(UTC) - timedelta(minutes=5)
    t1 = t0 + timedelta(seconds=120)

    # Use a session factory that yields independent sessions bound to same DB
    engine = db_session.get_bind()
    session_factory = sessionmaker(bind=engine)

    with patch("herald.services.performance_metrics.SessionLocal", side_effect=session_factory), patch(
        "herald.services.performance_metrics.settings.HERALD_METRICS_ENABLED", True
    ):
        # 1. First attempt: full synthesis (120 seconds wall time)
        record_stage_metric(
            job_id=job_id,
            stage="TTS_TOTAL",
            started_at=t0,
            finished_at=t1,
            duration_ms=120000,
            status="success",
            metadata_json={"chunks_count": 4, "worker_id": "w1", "full_synthesis": True},
        )

        m1 = (
            db_session.query(JobProcessingMetric)
            .filter(JobProcessingMetric.job_id == job_id, JobProcessingMetric.stage == "TTS_TOTAL")
            .first()
        )
        assert m1 is not None
        assert m1.duration_ms == 120000
        assert m1.metadata_json["full_synthesis"] is True

        # 2. Downstream failure requeues job -> retry reuses all completed chunks (near-zero 200ms pass)
        t_retry_start = datetime.now(UTC)
        t_retry_finish = t_retry_start + timedelta(milliseconds=200)

        record_stage_metric(
            job_id=job_id,
            stage="TTS_TOTAL",
            started_at=t_retry_start,
            finished_at=t_retry_finish,
            duration_ms=200,
            status="success",
            metadata_json={"chunks_count": 4, "worker_id": "w1", "full_synthesis": False},
        )

        db_session.expire_all()
        m_after = (
            db_session.query(JobProcessingMetric)
            .filter(JobProcessingMetric.job_id == job_id, JobProcessingMetric.stage == "TTS_TOTAL")
            .first()
        )
        # Critical assertion: duration_ms MUST remain 120,000 ms, NOT overwritten by 200 ms!
        assert m_after.duration_ms == 120000
        assert m_after.metadata_json["full_synthesis"] is True

    # 3. Create completed job and verify calculate_job_eta uses the 120s sample
    job = PodcastJob(
        id=job_id,
        transport="telegram",
        status=JobState.COMPLETE.value,
        source_hash="h_eta_1",
        source_text="Sample text for ETA",
        audio_duration_seconds=60,  # 60s audio, 120s wall time -> RTF = 2.0
        completed_at=datetime.now(UTC),
        script_json={"segments": [{"narration": "Test script narration text here."}]},
    )
    db_session.add(job)
    db_session.commit()

    target_job = PodcastJob(
        id="new-job-2222-3333-4444-555555555555",
        transport="telegram",
        status=JobState.QUEUED_TTS.value,
        source_hash="h_eta_2",
        source_text="Sample target text",
        custom_speed=1.0,
        script_json={"segments": [{"narration": "Test script narration text here."}]},
        created_at=datetime.now(UTC),
    )
    db_session.add(target_job)
    db_session.commit()

    eta = calculate_job_eta(db_session, target_job)
    assert eta["rtf_source"] == "historical"
    assert eta["realtime_factor"] == 2.0  # Derived from 120,000 / 60,000, not contaminated by 200ms!
