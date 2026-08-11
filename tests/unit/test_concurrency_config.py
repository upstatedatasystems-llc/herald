from herald.concurrency import (
    ConcurrencyConfig,
    detect_cpus,
    get_semaphores,
    resolve_concurrency_settings,
)
from herald.tts.chunk_manager import compute_chunk_text_hash


def test_detect_cpus():
    cpus = detect_cpus()
    assert isinstance(cpus, int)
    assert cpus >= 1


def test_profile_single_forced():
    cfg = resolve_concurrency_settings(profile="single", cpus_override=8)
    assert cfg.profile == "single"
    assert cfg.detected_cpus == 8
    assert cfg.worker_concurrency == 1
    assert cfg.script_concurrency == 1
    assert cfg.tts_global_slots == 1
    assert cfg.tts_per_job == 1
    assert cfg.ffmpeg_concurrency == 1
    assert cfg.n8n_concurrency == 1


def test_profile_auto_1_cpu():
    cfg = resolve_concurrency_settings(profile="auto", cpus_override=1)
    assert cfg.worker_concurrency == 1
    assert cfg.script_concurrency == 1
    assert cfg.tts_global_slots == 1
    assert cfg.tts_per_job == 1
    assert cfg.ffmpeg_concurrency == 1
    assert cfg.n8n_concurrency == 1


def test_profile_auto_2_cpu():
    cfg = resolve_concurrency_settings(profile="auto", cpus_override=2)
    assert cfg.worker_concurrency == 1
    assert cfg.script_concurrency == 2
    assert cfg.tts_global_slots == 2
    assert cfg.tts_per_job == 2
    assert cfg.ffmpeg_concurrency == 1
    assert cfg.n8n_concurrency == 1


def test_profile_auto_4_cpu():
    cfg = resolve_concurrency_settings(profile="auto", cpus_override=4)
    assert cfg.worker_concurrency == 2
    assert cfg.script_concurrency == 3
    assert cfg.tts_global_slots == 3
    assert cfg.tts_per_job == 2
    assert cfg.ffmpeg_concurrency == 1
    assert cfg.n8n_concurrency == 1


def test_env_var_overrides():
    cfg = resolve_concurrency_settings(
        profile="single",
        worker_concurrency=4,
        tts_global_slots=6,
        cpus_override=1,
    )
    assert cfg.worker_concurrency == 4
    assert cfg.tts_global_slots == 6
    # Unset overrides remain profile defaults
    assert cfg.script_concurrency == 1
    assert cfg.tts_per_job == 1


def test_compute_chunk_text_hash():
    h1 = compute_chunk_text_hash("Hello world", "af_heart", 1.0)
    h2 = compute_chunk_text_hash("Hello world", "af_heart", 1.0)
    h3 = compute_chunk_text_hash("Hello world changed", "af_heart", 1.0)
    h4 = compute_chunk_text_hash("Hello world", "am_adam", 1.0)

    assert h1 == h2
    assert h1 != h3
    assert h1 != h4


def test_semaphores_acquisition():
    cfg = ConcurrencyConfig(
        profile="auto",
        detected_cpus=4,
        worker_concurrency=2,
        script_concurrency=3,
        tts_global_slots=3,
        tts_per_job=2,
        ffmpeg_concurrency=1,
        n8n_concurrency=2,
    )
    sem = get_semaphores(cfg)
    assert sem.global_tts.acquire(blocking=False) is True
    assert sem.script.acquire(blocking=False) is True
    assert sem.ffmpeg.acquire(blocking=False) is True

    # Release acquired semaphores
    sem.global_tts.release()
    sem.script.release()
    sem.ffmpeg.release()
