import logging
import os
import sys
import threading
import time
from typing import Any

logger = logging.getLogger("herald.resource_monitor")


def _read_proc_meminfo() -> dict[str, float]:
    """Read available and swap memory in MB from /proc/meminfo if available."""
    res = {"available_mb": 0.0, "swap_total_mb": 0.0, "swap_free_mb": 0.0, "swap_used_mb": 0.0}
    p = "/proc/meminfo"
    if not os.path.exists(p):
        return res
    try:
        data = {}
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    k = parts[0].strip()
                    v_str = parts[1].strip().split()[0]
                    if v_str.isdigit():
                        data[k] = float(v_str) / 1024.0  # kB to MB
        avail = data.get("MemAvailable", data.get("MemFree", 0.0))
        swap_total = data.get("SwapTotal", 0.0)
        swap_free = data.get("SwapFree", 0.0)
        res["available_mb"] = round(avail, 2)
        res["swap_total_mb"] = round(swap_total, 2)
        res["swap_free_mb"] = round(swap_free, 2)
        res["swap_used_mb"] = round(max(0.0, swap_total - swap_free), 2)
    except Exception as e:
        logger.debug(f"Could not read /proc/meminfo: {e}")
    return res


def _read_proc_self_status() -> float:
    """Read current process memory (VmRSS) in MB from /proc/self/status if available."""
    p = "/proc/self/status"
    if not os.path.exists(p):
        return 0.0
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    v_str = line.split(":")[1].strip().split()[0]
                    if v_str.isdigit():
                        return round(float(v_str) / 1024.0, 2)
    except Exception:
        pass
    return 0.0


def _read_cpu_times() -> tuple[float, float]:
    """Read total CPU active and idle jiffies from /proc/stat if available."""
    p = "/proc/stat"
    if not os.path.exists(p):
        return 0.0, 0.0
    try:
        with open(p, "r", encoding="utf-8") as f:
            first = f.readline()
            if first.startswith("cpu "):
                parts = [float(x) for x in first.split()[1:]]
                idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
                total = sum(parts)
                return total - idle, total
    except Exception:
        pass
    return 0.0, 0.0


class TTSResourceMonitor:
    """
    Low-overhead, low-privilege sampling monitor for Kokoro TTS synthesis.
    Samples CPU %, memory MB, and swap usage in memory every interval_seconds.
    """

    def __init__(self, interval_seconds: float = 5.0):
        self.interval_seconds = interval_seconds
        self.samples: list[dict[str, Any]] = []
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._start_time_mono: float = 0.0
        self._stop_time_mono: float = 0.0

    def start(self):
        """Start background sampling thread."""
        try:
            self._running = True
            self.samples.clear()
            self._start_time_mono = time.monotonic()
            self._thread = threading.Thread(
                target=self._sample_loop, daemon=True, name="herald-tts-monitor"
            )
            self._thread.start()
        except Exception as e:
            logger.warning(f"Could not start TTS resource monitor thread: {e}")

    def stop(self) -> dict[str, Any]:
        """Stop background sampling thread and return aggregated summary dictionary."""
        self._stop_time_mono = time.monotonic()
        self._running = False
        if self._thread and self._thread.is_alive():
            try:
                self._thread.join(timeout=2.0)
            except Exception:
                pass

        return self.get_aggregates()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False  # Do not suppress exceptions from the wrapped block

    def _sample_loop(self):
        last_active_jiffies, last_total_jiffies = _read_cpu_times()

        while self._running:
            time.sleep(self.interval_seconds)
            if not self._running:
                break

            try:
                now = time.monotonic()
                cur_active, cur_total = _read_cpu_times()
                cpu_pct = 0.0
                if cur_total > last_total_jiffies > 0:
                    d_active = cur_active - last_active_jiffies
                    d_total = cur_total - last_total_jiffies
                    cpu_pct = round(max(0.0, min(100.0, (d_active / d_total) * 100.0)), 2)

                last_active_jiffies, last_total_jiffies = cur_active, cur_total

                meminfo = _read_proc_meminfo()
                proc_mem_mb = _read_proc_self_status()

                # Fallback for Windows/dev environment without /proc
                if meminfo["available_mb"] == 0.0 and sys.platform == "win32":
                    try:
                        import psutil

                        vm = psutil.virtual_memory()
                        sw = psutil.swap_memory()
                        meminfo["available_mb"] = round(vm.available / (1024 * 1024), 2)
                        meminfo["swap_used_mb"] = round(sw.used / (1024 * 1024), 2)
                        proc_mem_mb = round(psutil.Process().memory_info().rss / (1024 * 1024), 2)
                        cpu_pct = round(psutil.cpu_percent(interval=None), 2)
                    except Exception:
                        pass

                sample = {
                    "timestamp_mono": now,
                    "cpu_percent": cpu_pct,
                    "proc_memory_mb": proc_mem_mb,
                    "available_memory_mb": meminfo["available_mb"],
                    "swap_used_mb": meminfo["swap_used_mb"],
                }

                with self._lock:
                    self.samples.append(sample)

            except Exception as e:
                logger.debug(f"Resource sampling error: {e}")

    def get_aggregates(self) -> dict[str, Any]:
        """Compute summary aggregates safely without throwing exceptions."""
        try:
            wall_ms = (
                max(0, int((self._stop_time_mono - self._start_time_mono) * 1000))
                if self._start_time_mono > 0
                else 0
            )
            with self._lock:
                samples_copy = list(self.samples)

            if not samples_copy:
                meminfo = _read_proc_meminfo()
                proc_mb = _read_proc_self_status()
                return {
                    "sample_count": 0,
                    "sample_interval_seconds": self.interval_seconds,
                    "avg_cpu_percent": 0.0,
                    "peak_cpu_percent": 0.0,
                    "peak_memory_mb": proc_mb,
                    "minimum_available_memory_mb": meminfo["available_mb"],
                    "swap_start_mb": meminfo["swap_used_mb"],
                    "swap_end_mb": meminfo["swap_used_mb"],
                    "swap_peak_mb": meminfo["swap_used_mb"],
                    "observed_tts_wall_time_ms": wall_ms,
                }

            cpu_vals = [s["cpu_percent"] for s in samples_copy]
            proc_mem_vals = [s["proc_memory_mb"] for s in samples_copy]
            avail_mem_vals = [
                s["available_memory_mb"] for s in samples_copy if s["available_memory_mb"] > 0
            ]
            swap_vals = [s["swap_used_mb"] for s in samples_copy]

            avg_cpu = round(sum(cpu_vals) / len(cpu_vals), 2) if cpu_vals else 0.0
            peak_cpu = round(max(cpu_vals), 2) if cpu_vals else 0.0
            peak_mem = round(max(proc_mem_vals), 2) if proc_mem_vals else 0.0
            min_avail = round(min(avail_mem_vals), 2) if avail_mem_vals else 0.0
            swap_start = round(swap_vals[0], 2) if swap_vals else 0.0
            swap_end = round(swap_vals[-1], 2) if swap_vals else 0.0
            swap_peak = round(max(swap_vals), 2) if swap_vals else 0.0

            return {
                "sample_count": len(samples_copy),
                "sample_interval_seconds": self.interval_seconds,
                "avg_cpu_percent": avg_cpu,
                "peak_cpu_percent": peak_cpu,
                "peak_memory_mb": peak_mem,
                "minimum_available_memory_mb": min_avail,
                "swap_start_mb": swap_start,
                "swap_end_mb": swap_end,
                "swap_peak_mb": swap_peak,
                "observed_tts_wall_time_ms": wall_ms,
            }
        except Exception as e:
            logger.warning(f"Failed to compute resource aggregates: {e}")
            return {
                "sample_count": 0,
                "sample_interval_seconds": self.interval_seconds,
                "avg_cpu_percent": 0.0,
                "peak_cpu_percent": 0.0,
                "peak_memory_mb": 0.0,
                "minimum_available_memory_mb": 0.0,
                "swap_start_mb": 0.0,
                "swap_end_mb": 0.0,
                "swap_peak_mb": 0.0,
                "observed_tts_wall_time_ms": 0,
            }
