import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Semaphore

logger = logging.getLogger("herald.concurrency")


def detect_cpus() -> int:
    """
    Detect the number of CPU cores available to the process,
    accounting for container cgroup quotas (cgroups v1/v2), scheduler affinity,
    and system CPU count fallback. Chooses the most restrictive finite limit.
    Fractional quotas are floored to full-core capacity, minimum 1.
    """
    detected_limits = []

    # 1. Check cgroups v2
    cgroup2_max = Path("/sys/fs/cgroup/cpu.max")
    if cgroup2_max.is_file():
        try:
            content = cgroup2_max.read_text().strip()
            parts = content.split()
            if len(parts) >= 2 and parts[0] != "max":
                quota = float(parts[0])
                period = float(parts[1])
                if period > 0:
                    val = math.floor(quota / period)
                    if val >= 1:
                        detected_limits.append(val)
                    else:
                        detected_limits.append(1)
        except Exception as e:
            logger.debug(f"Could not read cgroups v2 cpu.max: {e}")

    # 2. Check cgroups v1
    cgroup1_quota = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    cgroup1_period = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if cgroup1_quota.is_file() and cgroup1_period.is_file():
        try:
            quota = float(cgroup1_quota.read_text().strip())
            period = float(cgroup1_period.read_text().strip())
            if quota > 0 and period > 0:
                val = math.floor(quota / period)
                if val >= 1:
                    detected_limits.append(val)
                else:
                    detected_limits.append(1)
        except Exception as e:
            logger.debug(f"Could not read cgroups v1 quota/period: {e}")

    # 3. Check os.sched_getaffinity if available (Linux)
    if hasattr(os, "sched_getaffinity"):
        try:
            affinity_cpus = len(os.sched_getaffinity(0))
            if affinity_cpus >= 1:
                detected_limits.append(affinity_cpus)
        except Exception as e:
            logger.debug(f"sched_getaffinity failed: {e}")

    # 4. Check os.cpu_count()
    try:
        count = os.cpu_count()
        if count and count >= 1:
            detected_limits.append(count)
    except Exception:
        pass

    if detected_limits:
        return max(1, min(detected_limits))

    return 1


@dataclass
class ConcurrencyConfig:
    profile: str
    detected_cpus: int
    worker_concurrency: int
    script_concurrency: int
    tts_global_slots: int
    tts_per_job: int
    ffmpeg_concurrency: int
    n8n_concurrency: int

    def log_diagnostics(self):
        logger.info("=== Herald Concurrency Profile Diagnostics ===")
        logger.info(f"Herald concurrency profile: {self.profile}")
        logger.info(f"Detected CPUs: {self.detected_cpus}")
        logger.info(f"Worker concurrency: {self.worker_concurrency}")
        logger.info(f"Script concurrency: {self.script_concurrency}")
        logger.info(f"Global TTS slots: {self.tts_global_slots}")
        logger.info(f"TTS per job: {self.tts_per_job}")
        logger.info(f"FFmpeg concurrency: {self.ffmpeg_concurrency}")
        logger.info(f"n8n production concurrency: {self.n8n_concurrency}")
        logger.info("===============================================")


def resolve_concurrency_settings(
    profile: str = "auto",
    worker_concurrency: int | None = None,
    script_concurrency: int | None = None,
    tts_global_slots: int | None = None,
    tts_per_job: int | None = None,
    ffmpeg_concurrency: int | None = None,
    n8n_concurrency: int | None = None,
    cpus_override: int | None = None,
) -> ConcurrencyConfig:
    """
    Resolve effective concurrency settings based on profile name, detected CPUs,
    and explicit environment variable overrides.
    """
    profile_clean = (profile or "auto").strip().lower()
    if profile_clean not in ("single", "balanced", "auto"):
        logger.warning(f"Unknown concurrency profile '{profile}', falling back to 'auto'.")
        profile_clean = "auto"

    detected_cpus = cpus_override if cpus_override is not None else detect_cpus()
    detected_cpus = max(1, detected_cpus)

    if profile_clean == "single" or detected_cpus <= 1:
        w = 1
        s = 1
        gt = 1
        tpj = 1
        ff = 1
    elif detected_cpus == 2:
        w = 1
        s = 2
        gt = 2
        tpj = 2
        ff = 1
    elif detected_cpus <= 4:
        w = 2
        s = 3
        gt = 3
        tpj = 2
        ff = 1
    else:
        w = min(4, max(2, detected_cpus // 2))
        s = min(6, max(3, detected_cpus))
        gt = min(6, max(3, detected_cpus - 1))
        tpj = min(4, max(2, detected_cpus // 2))
        ff = min(2, max(1, detected_cpus // 4))

    # n8n production concurrency defaults to 1 unless explicitly overridden
    n8n = 1

    # Apply explicit overrides if provided and > 0
    if worker_concurrency is not None and worker_concurrency > 0:
        w = worker_concurrency
    if script_concurrency is not None and script_concurrency > 0:
        s = script_concurrency
    if tts_global_slots is not None and tts_global_slots > 0:
        gt = tts_global_slots
    if tts_per_job is not None and tts_per_job > 0:
        tpj = tts_per_job
    if ffmpeg_concurrency is not None and ffmpeg_concurrency > 0:
        ff = ffmpeg_concurrency
    if n8n_concurrency is not None and n8n_concurrency > 0:
        n8n = n8n_concurrency

    # Ensure all settings are at least 1
    return ConcurrencyConfig(
        profile=profile_clean,
        detected_cpus=detected_cpus,
        worker_concurrency=max(1, w),
        script_concurrency=max(1, s),
        tts_global_slots=max(1, gt),
        tts_per_job=max(1, tpj),
        ffmpeg_concurrency=max(1, ff),
        n8n_concurrency=max(1, n8n),
    )


class ConcurrencySemaphores:
    """Thread-safe semaphores initialized from ConcurrencyConfig."""

    def __init__(self, config: ConcurrencyConfig):
        self.config = config
        self.global_tts = Semaphore(config.tts_global_slots)
        self.script = Semaphore(config.script_concurrency)
        self.ffmpeg = Semaphore(config.ffmpeg_concurrency)

    def create_per_job_tts_semaphore(self) -> Semaphore:
        return Semaphore(self.config.tts_per_job)


_GLOBAL_SEMAPHORES: ConcurrencySemaphores | None = None


def initialize_semaphores(config: ConcurrencyConfig | None = None) -> ConcurrencySemaphores:
    """
    Initialize the process-global semaphore set once.
    Subsequent calls return the existing singleton instance without replacing active semaphores.
    """
    global _GLOBAL_SEMAPHORES
    if _GLOBAL_SEMAPHORES is None:
        if config is None:
            from herald.config import settings

            config = settings.get_concurrency_config()
        _GLOBAL_SEMAPHORES = ConcurrencySemaphores(config)
    return _GLOBAL_SEMAPHORES


def get_semaphores(config: ConcurrencyConfig | None = None) -> ConcurrencySemaphores:
    """
    Get the process-global semaphore set.
    """
    global _GLOBAL_SEMAPHORES
    if _GLOBAL_SEMAPHORES is None:
        return initialize_semaphores(config)
    return _GLOBAL_SEMAPHORES


def reset_semaphores_for_tests():
    """Reset global semaphores singleton (for unit testing purposes only)."""
    global _GLOBAL_SEMAPHORES
    _GLOBAL_SEMAPHORES = None
