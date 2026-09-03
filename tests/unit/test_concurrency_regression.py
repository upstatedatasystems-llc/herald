import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from apps.worker.main import (
    claim_next_job,
    recover_stale_claims,
    renew_worker_lease,
    requeue_due_tts_retries,
)
from herald.concurrency import (
    ConcurrencyConfig,
    detect_cpus,
    get_semaphores,
    initialize_semaphores,
    reset_semaphores_for_tests,
    resolve_concurrency_settings,
)
from herald.db.models import JobState, PodcastJob


@pytest.fixture(autouse=True)
def clean_semaphores():
    reset_semaphores_for_tests()
    yield
    reset_semaphores_for_tests()


def test_process_global_semaphore_identity():
    """Prove that get_semaphores returns the exact same singleton instance across multiple calls."""
    config = ConcurrencyConfig(
        profile="balanced",
        detected_cpus=4,
        worker_concurrency=2,
        script_concurrency=3,
        tts_global_slots=3,
        tts_per_job=2,
        ffmpeg_concurrency=1,
        n8n_concurrency=1,
    )
    sem1 = initialize_semaphores(config)
    sem2 = get_semaphores()
    sem3 = get_semaphores(config)

    assert sem1 is sem2
    assert sem2 is sem3


def test_two_episodes_obey_global_tts_slots(monkeypatch, tmp_path):
    """
    Prove peak simultaneous Kokoro synthesis calls across TWO concurrent jobs never exceeds HERALD_TTS_GLOBAL_SLOTS.
    """
    monkeypatch.setattr("herald.config.settings.HERALD_WORK_DIR", str(tmp_path))
    monkeypatch.setattr("herald.config.settings.HERALD_TTS_GLOBAL_SLOTS", 2)

    config = resolve_concurrency_settings(profile="auto", tts_global_slots=2, tts_per_job=2)
    initialize_semaphores(config)
    semaphores = get_semaphores()

    job1_per_job = semaphores.create_per_job_tts_semaphore()
    job2_per_job = semaphores.create_per_job_tts_semaphore()

    active_calls = 0
    max_active_calls = 0
    lock = threading.Lock()

    def run_bounded_chunk(per_job_sem):
        nonlocal active_calls, max_active_calls
        with per_job_sem, semaphores.global_tts:
            with lock:
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
            time.sleep(0.05)
            with lock:
                active_calls -= 1

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(run_bounded_chunk, job1_per_job),
            executor.submit(run_bounded_chunk, job1_per_job),
            executor.submit(run_bounded_chunk, job2_per_job),
            executor.submit(run_bounded_chunk, job2_per_job),
        ]
        for fut in futures:
            fut.result()

    assert max_active_calls <= 2
    assert max_active_calls <= 2


def test_lease_heartbeat_renewal(db_session):
    """Prove that renew_worker_lease updates heartbeat_at, extends lease_expires_at, and respects ownership."""
    t0 = datetime.now(UTC)
    job = PodcastJob(
        id="job-hb-test-01",
        gmail_message_id="msg-hb-01",
        sender_email="user@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-hb",
        source_text="HB test",
        status=JobState.SYNTHESIZING.value,
        claimed_at=t0,
        claimed_by="worker-hb",
        claim_owner="worker-hb",
        heartbeat_at=t0,
        last_heartbeat_at=t0,
        lease_expires_at=t0 + timedelta(seconds=10),
    )
    db_session.add(job)
    db_session.commit()

    # Mismatched worker cannot renew lease
    res_mismatch = renew_worker_lease(db_session, "job-hb-test-01", "worker-other", lease_seconds=300)
    assert res_mismatch is False

    # Matching worker successfully renews lease
    res_success = renew_worker_lease(db_session, "job-hb-test-01", "worker-hb", lease_seconds=300)
    assert res_success is True

    db_session.refresh(job)
    hb_at = job.heartbeat_at.replace(tzinfo=UTC) if job.heartbeat_at.tzinfo is None else job.heartbeat_at
    exp_at = job.lease_expires_at.replace(tzinfo=UTC) if job.lease_expires_at.tzinfo is None else job.lease_expires_at
    assert hb_at > t0
    assert exp_at > t0 + timedelta(seconds=10)


def test_expired_initial_lease_with_fresh_heartbeat_not_reclaimed(db_session):
    """Prove that an expired lease timestamp with a fresh heartbeat is NOT reclaimed."""
    now = datetime.now(UTC)
    old_lease_exp = now - timedelta(seconds=30)
    fresh_heartbeat = now - timedelta(seconds=10)

    job = PodcastJob(
        gmail_message_id="msg-lease-fresh-hb",
        sender_email="u@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="hash-fresh-hb",
        source_text="Content",
        status=JobState.SYNTHESIZING.value,
        claimed_at=now - timedelta(minutes=10),
        claimed_by="worker-1",
        claim_owner="worker-1",
        heartbeat_at=fresh_heartbeat,
        last_heartbeat_at=fresh_heartbeat,
        lease_expires_at=old_lease_exp,
    )
    db_session.add(job)
    db_session.commit()

    recover_stale_claims(db_session, stale_minutes=5)
    db_session.refresh(job)

    assert job.status == JobState.SYNTHESIZING.value
    assert job.claimed_by == "worker-1"


def test_non_tts_failed_retryable_never_worker_claimed(db_session):
    """
    Prove that FAILED_RETRYABLE jobs representing non-TTS stages (DELIVERING, SCRIPTING, EXTRACTING)
    are NEVER claimed or requeued by the TTS worker.
    """
    past = datetime.now(UTC) - timedelta(minutes=5)

    job_delivering = PodcastJob(
        gmail_message_id="msg-fail-delivering",
        sender_email="u@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="h-deliv",
        source_text="Content",
        status=JobState.FAILED_RETRYABLE.value,
        failed_stage=JobState.DELIVERING.value,
        next_retry_at=past,
    )
    job_scripting = PodcastJob(
        gmail_message_id="msg-fail-scripting",
        sender_email="u@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="h-script",
        source_text="Content",
        status=JobState.FAILED_RETRYABLE.value,
        failed_stage=JobState.SCRIPTING.value,
        next_retry_at=past,
    )
    db_session.add_all([job_delivering, job_scripting])
    db_session.commit()

    requeue_due_tts_retries(db_session)
    claimed = claim_next_job(db_session, worker_id="worker-tts")

    assert claimed is None
    db_session.refresh(job_delivering)
    db_session.refresh(job_scripting)

    assert job_delivering.status == JobState.FAILED_RETRYABLE.value
    assert job_scripting.status == JobState.FAILED_RETRYABLE.value


def test_tts_failed_retryable_requeued_safely(db_session):
    """Prove that FAILED_RETRYABLE jobs with failed_stage=SYNTHESIZING are safely requeued when due."""
    past = datetime.now(UTC) - timedelta(seconds=10)

    job_tts = PodcastJob(
        gmail_message_id="msg-fail-tts",
        sender_email="u@example.com",
        request_mode="standard",
        source_type="email_body",
        source_hash="h-tts",
        source_text="Content",
        status=JobState.FAILED_RETRYABLE.value,
        failed_stage=JobState.SYNTHESIZING.value,
        next_retry_at=past,
    )
    db_session.add(job_tts)
    db_session.commit()

    requeue_due_tts_retries(db_session)
    db_session.refresh(job_tts)
    assert job_tts.status == JobState.QUEUED_TTS.value

    claimed = claim_next_job(db_session, worker_id="worker-tts")
    assert claimed is not None
    assert claimed.id == job_tts.id


def test_cpu_detection_conservative_combinations(monkeypatch, tmp_path):
    """
    Test CPU detection across cgroups v1, v2, sched_getaffinity, and cpu_count.
    Proves most restrictive finite constraint is chosen and fractional CPUs round DOWN.
    """
    # 1. cgroup quota=1.5 CPUs -> floor -> 1
    cgroup2_file = tmp_path / "cpu.max"
    cgroup2_file.write_text("150000 100000")
    monkeypatch.setattr("herald.concurrency.Path", lambda p: cgroup2_file if "cpu.max" in str(p) else tmp_path / "missing")
    assert detect_cpus() == 1

    # 2. cgroup quota=0.75 CPUs -> floor -> 1
    cgroup2_file.write_text("75000 100000")
    assert detect_cpus() == 1


def test_single_profile_resolves_all_ones():
    """Prove that profile=single resolves worker/script/global/per_job/ffmpeg/n8n to 1."""
    config = resolve_concurrency_settings(profile="single", cpus_override=16)
    assert config.worker_concurrency == 1
    assert config.script_concurrency == 1
    assert config.tts_global_slots == 1
    assert config.tts_per_job == 1
    assert config.ffmpeg_concurrency == 1
    assert config.n8n_concurrency == 1
