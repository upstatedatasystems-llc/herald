import time
import pytest
from herald.services.resource_monitor import TTSResourceMonitor


def test_resource_monitor_sampling():
    monitor = TTSResourceMonitor(interval_seconds=0.1)
    monitor.start()

    # Perform dummy work
    total = sum(i * i for i in range(10000))
    time.sleep(0.35)

    aggregates = monitor.stop()
    assert aggregates["sample_count"] >= 1
    assert aggregates["observed_tts_wall_time_ms"] > 0
    assert "avg_cpu_percent" in aggregates
    assert "peak_memory_mb" in aggregates
    assert "minimum_available_memory_mb" in aggregates


def test_resource_monitor_context_manager():
    with TTSResourceMonitor(interval_seconds=0.1) as monitor:
        time.sleep(0.25)

    aggregates = monitor.get_aggregates()
    assert aggregates["sample_count"] >= 1
    assert aggregates["observed_tts_wall_time_ms"] > 0
