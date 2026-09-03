import concurrent.futures
import time

from herald.concurrency import reset_semaphores_for_tests, tts_slot_lock
from herald.config import settings


def test_tts_slot_lock_limits_concurrency(monkeypatch):
    """Test that tts_slot_lock bounds concurrent executions to configured slot count."""
    from herald.concurrency import initialize_semaphores

    reset_semaphores_for_tests()
    monkeypatch.setattr(settings, "HERALD_TTS_GLOBAL_SLOTS", 2)
    initialize_semaphores(settings.get_concurrency_config())

    active_count = 0
    max_active = 0
    import threading
    lock = threading.Lock()

    def worker():
        nonlocal active_count, max_active
        with tts_slot_lock(db=None, timeout_seconds=5.0):
            with lock:
                active_count += 1
                if active_count > max_active:
                    max_active = active_count
            time.sleep(0.05)
            with lock:
                active_count -= 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(worker) for _ in range(6)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    assert max_active <= 2
    assert active_count == 0
    reset_semaphores_for_tests()
