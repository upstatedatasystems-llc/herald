import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup


class SSRFVulnerabilityError(Exception):
    """Raised when a URL resolves to a prohibited internal or private IP address."""


class ArticleExtractionError(Exception):
    """Raised when an article URL cannot be fetched or contains insufficient content."""


def is_ip_allowed(ip_str: str) -> bool:
    """
    Check if an IP address is a safe public IP (not loopback, private, link-local, multicast, or metadata).
    Supports IPv4 and IPv6 addresses.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False

    # Explicit cloud metadata check (AWS/GCP/Azure IPv4 & IPv6 metadata endpoints)
    if ip_str in ("169.254.169.254", "fd00:ec2::254"):
        return False

    return True


def validate_url_host(url: str) -> tuple[str, int, str]:
    """
    Validate URL scheme, reject embedded credentials/localhost, resolve host IP address to prevent SSRF attacks.
    Returns tuple of (hostname, port, resolved_ip).
    """
    parsed = urlparse(url)

    if parsed.scheme.lower() not in ("http", "https"):
        raise SSRFVulnerabilityError(f"Unsupported scheme: '{parsed.scheme}'. Only HTTP and HTTPS are permitted.")

    if parsed.username or parsed.password:
        raise SSRFVulnerabilityError("URLs with embedded user credentials are not permitted.")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFVulnerabilityError("Invalid URL: missing hostname.")

    if hostname.lower() in ("localhost", "localhost.localdomain"):
        raise SSRFVulnerabilityError("Access to localhost is strictly prohibited.")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)

    try:
        addr_info = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFVulnerabilityError(f"DNS resolution failed for hostname '{hostname}': {e}")

    resolved_ip = None
    for family, socktype, proto, canonname, sockaddr in addr_info:
        ip_str = sockaddr[0]
        if not is_ip_allowed(ip_str):
            raise SSRFVulnerabilityError(
                f"Security Violation: Target host '{hostname}' resolves to prohibited IP address '{ip_str}'"
            )
        if not resolved_ip:
            resolved_ip = ip_str

    if not resolved_ip:
        raise SSRFVulnerabilityError(f"Could not resolve valid IP for host '{hostname}'")

    return hostname, port, resolved_ip


def extract_article_from_url(
    url: str,
    timeout_seconds: float = 10.0,
    max_bytes: int = 5_000_000,
    max_redirects: int = 3,
) -> tuple[str, str, str]:
    """
    Safely extract article title, canonical text, and canonical URL from a public web page.
    Enforces strict SSRF protections, streaming size limits, redirect limits, and response timeouts.

    Returns:
        Tuple of (title, extracted_text, canonical_url)
    """
    current_url = url

    headers = {
        "User-Agent": "Herald-Podcast-Agent/1.0 (+https://github.com/upstatedatasystems-llc/herald)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Initial SSRF check on target URL
    hostname, port, target_ip = validate_url_host(current_url)

    # Use HTTP transport with explicit connect and read timeouts
    timeouts = httpx.Timeout(timeout_seconds, connect=5.0)

    with httpx.Client(follow_redirects=False, timeout=timeouts, headers=headers) as client:
        redirect_count = 0

        while redirect_count <= max_redirects:
            try:
                # Stream response to enforce max response size limit without downloading oversized bodies
                with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        redirect_count += 1
                        if redirect_count > max_redirects:
                            raise ArticleExtractionError(f"Exceeded maximum redirect limit of {max_redirects}")

                        location = response.headers.get("location")
                        if not location:
                            raise ArticleExtractionError("Redirect response missing Location header")

                        current_url = str(response.url.join(location))
                        # Re-enforce SSRF validation on target redirect URL
                        validate_url_host(current_url)
                        continue

                    if response.status_code != 200:
                        raise ArticleExtractionError(f"Server returned non-200 status code: {response.status_code}")

                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" not in content_type and "text/plain" not in content_type:
                        raise ArticleExtractionError(f"Unsupported content type '{content_type}'. Expected HTML or plain text.")

                    # Read body in chunks up to max_bytes limit
                    body_chunks = []
                    bytes_read = 0
                    for chunk in response.iter_bytes(chunk_size=8192):
                        bytes_read += len(chunk)
                        if bytes_read > max_bytes:
                            raise ArticleExtractionError(f"Response size exceeds maximum limit of {max_bytes} bytes")
                        body_chunks.append(chunk)

                    content_bytes = b"".join(body_chunks)
                    break

            except httpx.HTTPError as e:
                raise ArticleExtractionError(f"HTTP request failed for '{current_url}': {e}")

    # Parse HTML content
    html_text = content_bytes.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html_text, "html.parser")

    # Extract title
    title_tag = soup.find("title")
    title = title_tag.get_text().strip() if title_tag else "Extracted Article"

    # Clean non-content elements
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "noscript"]):
        tag.extract()

    # Prefer article or main tag
    main_container = soup.find("article") or soup.find("main") or soup.find("body") or soup
    paragraphs = main_container.find_all(["p", "h1", "h2", "h3", "h4", "li"])

    extracted_lines = []
    for p in paragraphs:
        p_text = p.get_text().strip()
        if len(p_text) > 15:
            extracted_lines.append(p_text)

    full_text = "\n\n".join(extracted_lines)

    if len(full_text.strip()) < 100:
        raise ArticleExtractionError("Insufficient article text extracted from page (less than 100 characters).")

    canonical_url = current_url
    canonical_tag = soup.find("link", rel="canonical")
    if canonical_tag and canonical_tag.get("href"):
        canonical_url = canonical_tag["href"]

    return title, full_text, canonical_url
