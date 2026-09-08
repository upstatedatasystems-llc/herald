"""
Stage-Aware Failure Diagnostics & Anti-SSRF Protection.

Collects in-container diagnostics at failure origin, strictly validates target IPs
against SSRF rules prior to connection, records network and resolver context,
preserves multi-attempt histories in PodcastJob.auto_diagnostics_json, and
formats concise summaries for Telegram error notifications.
"""

from datetime import UTC, datetime
import ipaddress
import logging
import os
from pathlib import Path
import socket
import ssl
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


def _probe_network_target(
    target_url: str,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """
    Perform a safe, stage-aware network probe against target_url with strict anti-SSRF protections.
    Resolves DNS, validates all IPs with is_ip_allowed, and connects directly to the validated IP.
    Aborts immediately on SSRF violations without opening TCP sockets.
    """
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

    # 1. DNS Resolution
    try:
        addr_info = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        return {
            "status": "DNS_FAILURE",
            "hostname": hostname,
            "port": port,
            "dns_status": "FAILED",
            "dns_error": str(e),
            "summary": f"DNS: Failed for {hostname} ({e})",
        }
    except Exception as e:
        return {
            "status": "DNS_FAILURE",
            "hostname": hostname,
            "port": port,
            "dns_status": "FAILED",
            "dns_error": str(e),
            "summary": f"DNS: Error for {hostname} ({e})",
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

    primary_ip = resolved_ips[0]

    # 3. Direct TCP Connection to pre-validated IP
    family = socket.AF_INET6 if ":" in primary_ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(min(timeout_seconds, 2.0))
    try:
        sock.connect((primary_ip, port))
    except Exception as e:
        sock.close()
        return {
            "status": "TCP_FAILURE",
            "hostname": hostname,
            "port": port,
            "resolved_ips": resolved_ips,
            "connected_ip": primary_ip,
            "dns_status": "SUCCESS",
            "tcp_status": "FAILED",
            "tcp_error": str(e),
            "summary": f"DNS: OK • TCP: Failed ({primary_ip}:{port} - {e})",
        }

    # 4. TLS Handshake if HTTPS
    if scheme == "https":
        try:
            context = ssl.create_default_context()
            tls_sock = context.wrap_socket(sock, server_hostname=hostname)
            tls_sock.close()
        except Exception as e:
            sock.close()
            return {
                "status": "TLS_FAILURE",
                "hostname": hostname,
                "port": port,
                "resolved_ips": resolved_ips,
                "connected_ip": primary_ip,
                "dns_status": "SUCCESS",
                "tcp_status": "SUCCESS",
                "tls_status": "FAILED",
                "tls_error": str(e),
                "summary": f"DNS: OK • TCP: OK • TLS: Failed ({hostname} - {e})",
            }
    else:
        sock.close()

    return {
        "status": "SUCCESS",
        "hostname": hostname,
        "port": port,
        "resolved_ips": resolved_ips,
        "connected_ip": primary_ip,
        "dns_status": "SUCCESS",
        "tcp_status": "SUCCESS",
        "tls_status": "SUCCESS" if scheme == "https" else "N/A",
        "summary": "DNS: OK • TCP: OK" + (" • TLS: OK" if scheme == "https" else ""),
    }


def collect_failure_diagnostics(
    stage: str,
    error: Exception | str,
    target_url: str | None = None,
    job_id: str | None = None,
    attempt: int = 1,
    db: Session | None = None,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """
    Collect stage-aware failure diagnostics at failure boundary.
    Safely executes probes, preserves multi-attempt history in DB, and generates error summary.
    """
    error_cat, safe_msg = sanitize_error(error)
    error_type = type(error).__name__ if isinstance(error, Exception) else "Error"

    probe_result: dict[str, Any] | None = None
    summary = ""

    # Check for HTTP 403 in error message or type
    err_str = str(error).lower()
    is_http_403 = "403" in err_str or "forbidden" in err_str or error_cat == "SOURCE_ACCESS_BLOCKED"

    if stage in ("extraction", "url_fetch") and target_url:
        probe_result = _probe_network_target(target_url, timeout_seconds=timeout_seconds)
        summary = probe_result.get("summary", "")
        if is_http_403:
            summary = f"HTTP 403: Publisher blocked automated retrieval ({probe_result.get('summary', '')})"
    elif stage in ("ai_script", "gemini", "research"):
        # Model diagnostics (no network probe against URL)
        if error_cat == "AI_MODEL_UNAVAILABLE":
            summary = f"AI: Model Unavailable (404) - {safe_msg}"
        else:
            summary = f"AI: {error_cat} - {safe_msg}"
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
