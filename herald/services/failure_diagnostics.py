"""
Stage-Aware Failure Diagnostics & Anti-SSRF Protection.

Collects in-container diagnostics at failure origin, strictly validates target IPs
against SSRF rules prior to connection, records network and resolver context,
preserves multi-attempt histories in PodcastJob.auto_diagnostics_json, and
formats concise summaries for Telegram error notifications.
"""

from datetime import UTC, datetime
import html
import ipaddress
import logging
import os
from pathlib import Path
import socket
import ssl
import threading
import time
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from herald.config import settings
from herald.db.models import PodcastJob
from herald.extraction.url_extractor import is_ip_allowed
from herald.services.redaction import redact_dict, redact_text, sanitize_error

logger = logging.getLogger("herald.diagnostics.failure")


def _get_container_id() -> str:
    """Read container ID or hostname."""
    try:
        hostname_path = Path("/etc/hostname")
        if hostname_path.exists():
            return hostname_path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return socket.gethostname()


def _get_resolver_context() -> dict[str, Any]:
    """Capture resolver context (/etc/resolv.conf and configured DNS servers)."""
    context: dict[str, Any] = {
        "dns_primary": getattr(settings, "HERALD_DNS_PRIMARY", None),
        "dns_secondary": getattr(settings, "HERALD_DNS_SECONDARY", None),
        "resolv_conf_nameservers": [],
    }
    try:
        resolv_path = Path("/etc/resolv.conf")
        if resolv_path.exists():
            for line in resolv_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("nameserver "):
                    parts = stripped.split()
                    if len(parts) >= 2:
                        context["resolv_conf_nameservers"].append(parts[1])
    except Exception:
        pass
    return context


def _resolve_dns_bounded(
    hostname: str,
    port: int,
    timeout: float = 3.0,
) -> tuple[list[tuple] | None, Exception | None]:
    """
    Resolve DNS with an actual wall-clock bound.
    Runs in a daemon thread so that if resolution hangs, cleanup never blocks.
    """
    result: list[list[tuple]] = []
    error: list[Exception] = []
    done_event = threading.Event()

    def _worker():
        try:
            res = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
            result.append(res)
        except Exception as e:
            error.append(e)
        finally:
            done_event.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    eff_timeout = max(0.05, float(timeout))
    if not done_event.wait(timeout=eff_timeout):
        return None, TimeoutError(f"DNS resolution timed out after {eff_timeout:.1f}s")
    if error:
        return None, error[0]
    return (result[0] if result else []), None


def _probe_network_target(
    target_url: str,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """
    Perform a safe, stage-aware network probe against target_url with strict anti-SSRF protections.
    Bounded by total deadline <= 5.0s. Probes up to 3 candidate IPs, reporting per-address and overall status.
    Aborts immediately on SSRF violations without opening TCP sockets.
    """
    start_time = time.monotonic()
    total_deadline_sec = min(max(0.5, float(timeout_seconds)), 5.0)
    deadline = start_time + total_deadline_sec

    try:
        parsed = urlparse(target_url)
    except Exception as e:
        return {
            "status": "SSRF_REFUSAL",
            "error": f"Malformed URL: {e}",
            "summary": "SSRF: Refused (Malformed URL)",
        }

    scheme = (parsed.scheme or "").lower()
    hostname = parsed.hostname
    if scheme not in ("http", "https") or not hostname or hostname.lower() in ("localhost", "localhost.localdomain"):
        return {
            "status": "SSRF_REFUSAL",
            "error": f"Prohibited scheme or host: '{scheme}://{hostname}'",
            "summary": f"SSRF: Refused (Prohibited host '{hostname}')",
        }

    port = parsed.port or (443 if scheme == "https" else 80)

    # 1. DNS Resolution with wall-clock bound
    dns_rem_time = max(0.1, deadline - time.monotonic())
    addr_info, dns_err = _resolve_dns_bounded(hostname, port, timeout=dns_rem_time)
    if dns_err:
        is_timeout = isinstance(dns_err, TimeoutError)
        return {
            "status": "TIMEOUT" if is_timeout else "DNS_FAILURE",
            "hostname": hostname,
            "port": port,
            "dns_status": "TIMEOUT" if is_timeout else "FAILED",
            "dns_error": str(dns_err),
            "summary": f"DNS: Timed out for {hostname}" if is_timeout else f"DNS: Failed for {hostname} ({dns_err})",
        }

    resolved_ips: list[str] = []
    for family, socktype, proto, canonname, sockaddr in addr_info:
        ip_str = sockaddr[0]
        if ip_str not in resolved_ips:
            resolved_ips.append(ip_str)

    if not resolved_ips:
        return {
            "status": "DNS_FAILURE",
            "hostname": hostname,
            "port": port,
            "dns_status": "FAILED",
            "dns_error": "No IP addresses resolved",
            "summary": f"DNS: Failed (No IP resolved for {hostname})",
        }

    # 2. SSRF Validation - Validate ALL candidate IPs
    for ip_str in resolved_ips:
        if not is_ip_allowed(ip_str):
            return {
                "status": "SSRF_REFUSAL",
                "hostname": hostname,
                "port": port,
                "resolved_ips": resolved_ips,
                "prohibited_ip": ip_str,
                "summary": f"SSRF: Blocked prohibited IP {ip_str}",
            }

    # 3. Direct TCP & TLS Probing across candidate IPs (up to 3) bounded by deadline
    if deadline - time.monotonic() <= 0.05:
        return {
            "status": "TIMEOUT",
            "hostname": hostname,
            "port": port,
            "dns_status": "SUCCESS",
            "resolved_ips": resolved_ips,
            "summary": "DNS: OK • Timed out before TCP probe",
        }

    candidate_ips = resolved_ips[:3]
    per_ip_results: dict[str, Any] = {}
    last_tcp_error: str | None = None
    last_tls_error: str | None = None
    connected_ip: str | None = None

    for ip_str in candidate_ips:
        now_t = time.monotonic()
        rem_sec = deadline - now_t
        if rem_sec <= 0.1:
            per_ip_results[ip_str] = {"tcp": "SKIPPED", "error": "Deadline exceeded"}
            continue

        per_ip_timeout = min(rem_sec, 2.0)
        family = socket.AF_INET6 if ":" in ip_str else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(per_ip_timeout)

        ip_res: dict[str, Any] = {"ip": ip_str}
        tcp_ok = False
        try:
            sock.connect((ip_str, port))
            tcp_ok = True
            ip_res["tcp"] = "SUCCESS"
            if not connected_ip:
                connected_ip = ip_str
        except Exception as e:
            sock.close()
            last_tcp_error = str(e)
            ip_res["tcp"] = "FAILED"
            ip_res["tcp_error"] = str(e)
            per_ip_results[ip_str] = ip_res
            continue

        if scheme == "https" and tcp_ok:
            now_t2 = time.monotonic()
            rem_sec2 = deadline - now_t2
            if rem_sec2 <= 0.1:
                sock.close()
                ip_res["tls"] = "SKIPPED"
                per_ip_results[ip_str] = ip_res
                continue

            try:
                context = ssl.create_default_context()
                tls_sock = context.wrap_socket(sock, server_hostname=hostname)
                tls_sock.close()
                ip_res["tls"] = "SUCCESS"
            except Exception as e:
                sock.close()
                last_tls_error = str(e)
                ip_res["tls"] = "FAILED"
                ip_res["tls_error"] = str(e)
                per_ip_results[ip_str] = ip_res
                continue
        else:
            sock.close()
            ip_res["tls"] = "N/A"

        per_ip_results[ip_str] = ip_res

    probed_count = len(per_ip_results)
    tcp_success_count = sum(1 for r in per_ip_results.values() if r.get("tcp") == "SUCCESS")
    tls_success_count = sum(1 for r in per_ip_results.values() if r.get("tls") == "SUCCESS")
    tls_failed_count = sum(1 for r in per_ip_results.values() if r.get("tls") == "FAILED")

    if tcp_success_count == 0:
        overall_status = "TCP_FAILURE"
        tcp_status = "FAILED"
        tls_status = "N/A"
        summary = (
            f"DNS: OK • TCP: Failed ({candidate_ips[0]}:{port} - {last_tcp_error})"
            if probed_count == 1
            else f"DNS: OK • TCP: Failed ({probed_count} IPs unreachable - {last_tcp_error})"
        )
    elif scheme == "https" and tls_failed_count > 0 and tls_success_count == 0:
        overall_status = "TLS_FAILURE"
        tcp_status = "SUCCESS"
        tls_status = "FAILED"
        summary = f"DNS: OK • TCP: OK • TLS: Failed ({hostname} - {last_tls_error})"
    elif tcp_success_count < probed_count:
        overall_status = "PARTIAL_SUCCESS"
        tcp_status = "MIXED"
        tls_status = "SUCCESS" if (scheme != "https" or tls_success_count > 0) else "FAILED"
        summary = f"DNS: OK • TCP: Mixed ({tcp_success_count}/{probed_count} IPs reachable)"
    else:
        overall_status = "SUCCESS"
        tcp_status = "SUCCESS"
        tls_status = "SUCCESS" if scheme == "https" else "N/A"
        summary = "DNS: OK • TCP: OK" + (" • TLS: OK" if scheme == "https" else "")

    return {
        "status": overall_status,
        "hostname": hostname,
        "port": port,
        "resolved_ips": resolved_ips,
        "probed_ips": list(per_ip_results.keys()),
        "connected_ip": connected_ip or candidate_ips[0],
        "dns_status": "SUCCESS",
        "tcp_status": tcp_status,
        "tcp_error": last_tcp_error or "",
        "tls_status": tls_status,
        "tls_error": last_tls_error or "",
        "per_ip_results": per_ip_results,
        "summary": summary,
    }


def _probe_trusted_kokoro(timeout_seconds: float = 2.0) -> dict[str, Any]:
    """
    Perform safe, bounded network & health diagnostics against trusted internal Kokoro service.
    Restricted strictly to configured settings.KOKORO_API_URL (never user input).
    Allows expected private Docker network addresses.
    """
    api_url = getattr(settings, "KOKORO_API_URL", "http://kokoro:8880")
    try:
        parsed = urlparse(api_url)
    except Exception as e:
        return {
            "status": "CONFIG_ERROR",
            "error": f"Invalid KOKORO_API_URL '{api_url}': {e}",
            "summary": "Kokoro: Invalid API URL",
        }

    hostname = parsed.hostname or "kokoro"
    port = parsed.port or (443 if parsed.scheme == "https" else 8880)

    start_time = time.monotonic()
    total_deadline_sec = min(max(0.5, float(timeout_seconds)), 2.0)
    deadline = start_time + total_deadline_sec

    # 1. Bounded DNS resolution
    dns_deadline = max(0.1, deadline - time.monotonic())
    addr_info, dns_err = _resolve_dns_bounded(hostname, port, timeout=dns_deadline)
    if dns_err:
        is_timeout = isinstance(dns_err, TimeoutError)
        return {
            "status": "TIMEOUT" if is_timeout else "DNS_FAILURE",
            "hostname": hostname,
            "port": port,
            "dns_status": "TIMEOUT" if is_timeout else "FAILED",
            "dns_error": str(dns_err),
            "summary": f"Kokoro: DNS timed out ({hostname})" if is_timeout else f"Kokoro: DNS failed ({dns_err})",
        }

    resolved_ips: list[str] = []
    for family, socktype, proto, canonname, sockaddr in (addr_info or []):
        ip_str = sockaddr[0]
        if ip_str not in resolved_ips:
            resolved_ips.append(ip_str)

    if not resolved_ips:
        return {
            "status": "DNS_FAILURE",
            "hostname": hostname,
            "port": port,
            "dns_status": "FAILED",
            "dns_error": "No IP addresses resolved",
            "summary": f"Kokoro: No IP resolved for {hostname}",
        }

    # 2. Direct TCP connection probe to trusted internal IP
    rem_time = deadline - time.monotonic()
    if rem_time <= 0.05:
        return {
            "status": "TIMEOUT",
            "hostname": hostname,
            "port": port,
            "dns_status": "SUCCESS",
            "resolved_ips": resolved_ips,
            "summary": "Kokoro: DNS OK • Timed out before TCP probe",
        }

    target_ip = resolved_ips[0]
    sock_family = socket.AF_INET6 if ":" in target_ip else socket.AF_INET
    sock = socket.socket(sock_family, socket.SOCK_STREAM)
    sock.settimeout(min(1.5, rem_time))
    try:
        sock.connect((target_ip, port))
        sock.close()
        return {
            "status": "HEALTHY",
            "hostname": hostname,
            "target_ip": target_ip,
            "port": port,
            "tcp_status": "SUCCESS",
            "summary": f"Kokoro: Reachable ({target_ip}:{port})",
        }
    except TimeoutError as te:
        sock.close()
        return {
            "status": "TIMEOUT",
            "hostname": hostname,
            "target_ip": target_ip,
            "port": port,
            "tcp_status": "TIMEOUT",
            "tcp_error": str(te),
            "summary": f"Kokoro: Connection timed out ({target_ip}:{port})",
        }
    except ConnectionRefusedError as cre:
        sock.close()
        return {
            "status": "CONNECTION_REFUSED",
            "hostname": hostname,
            "target_ip": target_ip,
            "port": port,
            "tcp_status": "REFUSED",
            "tcp_error": str(cre),
            "summary": f"Kokoro: Connection refused ({target_ip}:{port})",
        }
    except Exception as ce:
        sock.close()
        err_msg = str(ce)
        st = "TIMEOUT" if "timed out" in err_msg.lower() else "CONNECTION_FAILED"
        return {
            "status": st,
            "hostname": hostname,
            "target_ip": target_ip,
            "port": port,
            "tcp_status": "FAILED",
            "tcp_error": err_msg,
            "summary": f"Kokoro: {err_msg} ({target_ip}:{port})",
        }


def collect_failure_diagnostics(
    stage: str,
    error: Exception | str,
    target_url: str | None = None,
    job_id: str | None = None,
    attempt: int = 1,
    db: Session | None = None,
    timeout_seconds: float = 3.0,
    provider: str | None = None,
    model: str | None = None,
    operation: str | None = None,
) -> dict[str, Any]:
    """
    Collect stage-aware failure diagnostics at failure boundary.
    Safely executes probes, preserves multi-attempt history in DB, captures structured AI fields,
    and generates concise error summary.
    """
    error_cat, safe_msg = sanitize_error(error)
    error_type = type(error).__name__ if isinstance(error, Exception) else "Error"

    probe_result: dict[str, Any] | None = None
    ai_diag: dict[str, Any] | None = None
    summary = ""

    # Check for HTTP 403 in error message or type
    err_str = str(error).lower()
    is_http_403 = "403" in err_str or "forbidden" in err_str or error_cat == "SOURCE_ACCESS_BLOCKED"

    if stage in ("extraction", "url_fetch") and target_url:
        probe_result = _probe_network_target(target_url, timeout_seconds=timeout_seconds)
        summary = probe_result.get("summary", "")
        if is_http_403:
            summary = f"HTTP 403: Publisher blocked automated retrieval ({probe_result.get('summary', '')})"
    elif stage in ("scripting", "ai_script", "gemini", "research", "ai"):
        if operation == "literal_script" or getattr(error, "mode", None) == "literal":
            ai_diag = None
            summary = f"Scripting: {error_cat} - {safe_msg}"
        else:
            if provider is not None:
                eff_prov = provider
            elif stage == "research":
                eff_prov = getattr(settings, "RESEARCH_PROVIDER", "gemini")
            else:
                eff_prov = getattr(settings, "AI_PROVIDER", "none")

            if model is not None:
                eff_model = model
            elif stage == "research" or (eff_prov == "gemini" and operation and "research" in str(operation)):
                eff_model = getattr(settings, "GEMINI_RESEARCH_MODEL", "gemini-3.6-flash")
            elif eff_prov == "gemini":
                eff_model = getattr(settings, "GEMINI_MODEL", "")
            else:
                eff_model = ""

            status_code = getattr(error, "status_code", None) or getattr(error, "http_status", None)
            is_retryable = error_cat in ("AI_RATE_LIMITED", "AI_TIMEOUT", "AI_SERVER_ERROR", "TEMPORARY_UNAVAILABLE")
            ai_diag = {
                "provider": eff_prov,
                "configured_model": eff_model,
                "http_status": status_code,
                "error_category": error_cat,
                "retryable": is_retryable,
            }
            if operation:
                ai_diag["operation"] = operation

            prov_disp = eff_prov.capitalize() if eff_prov else "AI"
            op_disp = f" ({operation})" if operation else ""
            model_disp = f" [{eff_model}]" if eff_model else ""
            if error_cat == "AI_MODEL_UNAVAILABLE":
                summary = f"{prov_disp}{op_disp}{model_disp}: Model Unavailable (404) - {safe_msg}"
            else:
                summary = f"{prov_disp}{op_disp}{model_disp}: {error_cat} - {safe_msg}"
    elif stage in ("tts", "kokoro"):
        probe_result = _probe_trusted_kokoro(timeout_seconds=min(timeout_seconds, 2.0))
        summary = f"TTS: {error_cat} ({probe_result.get('summary', '')})"
    elif stage in ("delivery", "telegram"):
        probe_url = target_url or "https://api.telegram.org"
        probe_result = _probe_network_target(probe_url, timeout_seconds=min(timeout_seconds, 2.0))
        summary = f"Delivery: {error_cat} ({probe_result.get('summary', '')})"
    else:
        summary = f"{stage.upper()}: {error_cat} - {safe_msg}"

    record: dict[str, Any] = {
        "attempt": attempt,
        "timestamp": datetime.now(UTC).isoformat(),
        "stage": stage,
        "error_type": error_type,
        "error_category": error_cat,
        "error_message": redact_text(safe_msg),
        "container_id": _get_container_id(),
        "resolver_context": _get_resolver_context(),
        "summary": redact_text(summary),
    }

    if target_url:
        record["target_url"] = redact_text(target_url)

    if probe_result:
        record["network_probe"] = probe_result

    if ai_diag:
        record["ai_diagnostics"] = ai_diag

    # Redact any accidental secret in the full dictionary
    sanitized_record = redact_dict(record)

    # Multi-attempt preservation in PodcastJob.auto_diagnostics_json
    if db and job_id:
        try:
            job = db.query(PodcastJob).filter(PodcastJob.id == job_id).first()
            if job:
                history = list(job.auto_diagnostics_json or [])
                history.append(sanitized_record)
                job.auto_diagnostics_json = history
                db.commit()
        except Exception as db_err:
            logger.warning(f"Failed to save auto_diagnostics_json for job '{job_id}': {db_err}")

    return sanitized_record


def format_concise_failure_summary(diag_record: dict[str, Any]) -> str:
    """Format a concise, user-friendly failure diagnostics line for Telegram notifications."""
    raw_summary = diag_record.get("summary") or diag_record.get("error_message") or ""
    if raw_summary:
        return f"• <b>Diagnostic:</b> {html.escape(str(raw_summary).strip())}"
    stage = html.escape(str(diag_record.get("stage") or "pipeline").upper())
    cat = html.escape(str(diag_record.get("error_category") or "ERROR"))
    return f"• <b>Diagnostic:</b> {stage} ({cat})"
