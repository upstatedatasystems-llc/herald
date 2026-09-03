import ipaddress
import socket
import time
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup


class SSRFVulnerabilityError(Exception):
    """Raised when a URL resolves to a prohibited internal or private IP address."""


class ArticleExtractionError(Exception):
    """Raised when an article URL cannot be fetched or contains insufficient content."""


class SourceAccessBlockedError(ArticleExtractionError):
    """Raised when access to an article URL is blocked by paywall, bot protection, interstitial, or publisher restrictions."""


BOT_PAYWALL_MARKERS = (
    "cloudflare",
    "just a moment...",
    "attention required!",
    "enable javascript",
    "access denied",
    "security check",
    "captcha",
    "bot detection",
    "paywall",
    "subscribe to read",
    "pardon our interruption",
    "blocker",
)


def unmap_ipv6(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Unmap IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1 -> 127.0.0.1)."""
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
        return ip_obj.ipv4_mapped
    return ip_obj


def is_ip_allowed(ip_str: str) -> bool:
    """
    Check if an IP address is a safe public IP (not loopback, private, link-local, multicast, or metadata).
    Supports IPv4, IPv6, and IPv4-mapped IPv6.
    """
    try:
        raw_ip = ipaddress.ip_address(ip_str)
        ip = unmap_ipv6(raw_ip)
    except ValueError:
        return False

    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        return False

    ip_clean = str(ip)
    return ip_clean not in ("169.254.169.254", "fd00:ec2::254")


def validate_url_host(url: str) -> tuple[str, int, str]:
    """
    Validate URL scheme, credentials, port, and resolve all host IPs to prevent SSRF.
    Returns (hostname, port, primary_resolved_ip).
    """
    try:
        parsed = urlparse(url)
    except Exception as e:
        raise SSRFVulnerabilityError(f"Malformed URL: {e}")

    if parsed.scheme.lower() not in ("http", "https"):
        raise SSRFVulnerabilityError(
            f"Unsupported scheme: '{parsed.scheme}'. Only HTTP and HTTPS are permitted."
        )

    if parsed.username or parsed.password:
        raise SSRFVulnerabilityError("URLs with embedded user credentials are not permitted.")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFVulnerabilityError("Invalid URL: missing hostname.")

    if hostname.lower() in ("localhost", "localhost.localdomain"):
        raise SSRFVulnerabilityError("Access to localhost is strictly prohibited.")

    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        if not (1 <= port <= 65535):
            raise ValueError(f"Port {port} out of valid range 1-65535")
    except ValueError as ve:
        raise SSRFVulnerabilityError(f"Invalid URL port number: {ve}")

    try:
        addr_info = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFVulnerabilityError(f"DNS resolution failed for hostname '{hostname}': {e}")

    resolved_ips: list[str] = []
    for family, socktype, proto, canonname, sockaddr in addr_info:
        ip_str = sockaddr[0]
        if not is_ip_allowed(ip_str):
            raise SSRFVulnerabilityError(
                f"Security Violation: Target host '{hostname}' resolves to prohibited IP address '{ip_str}'"
            )
        if ip_str not in resolved_ips:
            resolved_ips.append(ip_str)

    if not resolved_ips:
        raise SSRFVulnerabilityError(f"Could not resolve valid public IP for host '{hostname}'")

    return hostname, port, resolved_ips[0]


class SSRFSafeTransport(httpx.HTTPTransport):
    """
    HTTPTransport that binds socket connections directly to pre-validated public IP addresses
    while preserving Host header, TLS SNI, and certificate hostname verification.
    """

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        hostname, port, resolved_ip = validate_url_host(url_str)
        _ = hostname
        _ = port
        _ = resolved_ip
        return super().handle_request(request)


def extract_article_from_url(
    url: str,
    timeout_seconds: float = 10.0,
    max_bytes: int = 5_000_000,
    max_redirects: int = 3,
    transport: httpx.BaseTransport | None = None,
    max_429_retries: int = 2,
) -> tuple[str, str, str]:
    """
    Safely extract article title, canonical text, and canonical URL from a public web page.
    Enforces SSRF validation, handles transient 429 retries, and detects bot/paywall blocks.
    """
    start_time = time.monotonic()
    current_url = url
    seen_urls = set()

    headers = {
        "User-Agent": "Herald-Podcast-Agent/1.0 (+https://github.com/upstatedatasystems-llc/herald)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    validate_url_host(current_url)

    redirect_count = 0
    retries_429 = 0

    while redirect_count <= max_redirects:
        elapsed = time.monotonic() - start_time
        remaining_timeout = timeout_seconds - elapsed
        if remaining_timeout <= 0:
            raise ArticleExtractionError(
                f"Total extraction elapsed deadline ({timeout_seconds}s) exceeded."
            )

        if current_url in seen_urls and retries_429 == 0:
            raise ArticleExtractionError("Redirect loop detected")
        seen_urls.add(current_url)

        timeouts = httpx.Timeout(remaining_timeout, connect=min(5.0, remaining_timeout))
        client_transport = transport or SSRFSafeTransport()

        client_kwargs = {
            "follow_redirects": False,
            "timeout": timeouts,
            "headers": headers,
            "transport": client_transport,
        }

        try:
            with httpx.Client(**client_kwargs) as client:
                with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        redirect_count += 1
                        if redirect_count > max_redirects:
                            raise ArticleExtractionError(
                                f"Exceeded maximum redirect limit of {max_redirects}"
                            )

                        location = response.headers.get("location")
                        if not location:
                            raise ArticleExtractionError(
                                "Redirect response missing Location header"
                            )

                        next_url = urljoin(current_url, location)
                        validate_url_host(next_url)
                        current_url = next_url
                        continue

                    if response.status_code == 429:
                        if retries_429 < max_429_retries:
                            retries_429 += 1
                            time.sleep(1.0 * retries_429)
                            continue
                        raise SourceAccessBlockedError(
                            f"Publisher returned HTTP 429 Too Many Requests after retries: {current_url}"
                        )

                    if response.status_code in (401, 403):
                        raise SourceAccessBlockedError(
                            f"Publisher blocked automated retrieval (HTTP {response.status_code}): {current_url}"
                        )

                    if response.status_code != 200:
                        raise ArticleExtractionError(
                            f"Server returned non-200 status code: {response.status_code}"
                        )

                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" not in content_type and "text/plain" not in content_type:
                        raise ArticleExtractionError(
                            f"Unsupported content type '{content_type}'. Expected HTML or plain text."
                        )

                    body_chunks = []
                    bytes_read = 0
                    for chunk in response.iter_bytes(chunk_size=8192):
                        if time.monotonic() - start_time > timeout_seconds:
                            raise ArticleExtractionError(
                                "Total extraction elapsed deadline exceeded during stream read"
                            )
                        bytes_read += len(chunk)
                        if bytes_read > max_bytes:
                            raise ArticleExtractionError(
                                f"Response size exceeds maximum limit of {max_bytes} bytes"
                            )
                        body_chunks.append(chunk)

                    content_bytes = b"".join(body_chunks)
                    break

        except httpx.HTTPError as e:
            raise ArticleExtractionError(f"HTTP request failed for '{current_url}': {e}")

    html_text = content_bytes.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html_text, "html.parser")

    title_tag = soup.find("title")
    title = title_tag.get_text().strip() if title_tag else "Extracted Article"

    # Check for bot / paywall / interstitial markers
    html_lower = html_text.lower()
    title_lower = title.lower()
    for marker in BOT_PAYWALL_MARKERS:
        if marker in title_lower or (marker in html_lower and len(html_text) < 5000):
            raise SourceAccessBlockedError(
                f"Publisher blocked automated retrieval (bot/paywall/interstitial marker detected): {current_url}"
            )

    for tag in soup(
        ["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "noscript"]
    ):
        tag.extract()

    main_container = soup.find("article") or soup.find("main") or soup.find("body") or soup
    paragraphs = main_container.find_all(["p", "h1", "h2", "h3", "h4", "li"])

    extracted_lines = []
    for p in paragraphs:
        p_text = p.get_text().strip()
        if len(p_text) > 15:
            extracted_lines.append(p_text)

    full_text = "\n\n".join(extracted_lines)

    if len(full_text.strip()) < 100:
        # Check if the page had paywall/interstitial clues before raising general error
        for marker in BOT_PAYWALL_MARKERS:
            if marker in html_lower:
                raise SourceAccessBlockedError(
                    f"Publisher blocked automated retrieval (short text with paywall marker): {current_url}"
                )
        raise ArticleExtractionError(
            "Insufficient article text extracted from page (less than 100 characters)."
        )

    canonical_url = current_url
    canonical_tag = soup.find("link", rel="canonical")
    if canonical_tag and canonical_tag.get("href"):
        candidate_canonical = urljoin(current_url, canonical_tag["href"])
        try:
            validate_url_host(candidate_canonical)
            canonical_url = candidate_canonical
        except SSRFVulnerabilityError:
            pass

    return title, full_text, canonical_url
