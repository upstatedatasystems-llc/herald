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
    Check if an IP address is a safe public IP (not loopback, private, link-local, or metadata).
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_reserved:
        return False

    # Block AWS / GCP / Azure cloud metadata IP explicitly
    if ip_str == "169.254.169.254":
        return False

    return True


def validate_url_host(url: str) -> str:
    """
    Validate URL scheme and resolve host IP address to prevent SSRF attacks.
    Returns the resolved IP address string if safe.
    """
    parsed = urlparse(url)

    if parsed.scheme.lower() not in ("http", "https"):
        raise SSRFVulnerabilityError(f"Unsupported scheme: '{parsed.scheme}'. Only HTTP and HTTPS are permitted.")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFVulnerabilityError("Invalid URL: missing hostname.")

    # Reject explicit localhost strings
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        raise SSRFVulnerabilityError("Access to localhost is strictly prohibited.")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)

    try:
        addr_info = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFVulnerabilityError(f"DNS resolution failed for hostname '{hostname}': {e}")

    for family, socktype, proto, canonname, sockaddr in addr_info:
        ip_str = sockaddr[0]
        if not is_ip_allowed(ip_str):
            raise SSRFVulnerabilityError(
                f"Security Violation: Target host '{hostname}' resolves to prohibited IP address '{ip_str}'"
            )

    return addr_info[0][1]


def extract_article_from_url(
    url: str,
    timeout_seconds: float = 10.0,
    max_bytes: int = 5_000_000,
    max_redirects: int = 3,
) -> tuple[str, str, str]:
    """
    Safely extract article title, canonical text, and canonical URL from a public web page.
    Enforces strict SSRF protections, redirect limits, and response size bounds.

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
    validate_url_host(current_url)

    transport = httpx.HTTPTransport(retries=1)
    with httpx.Client(transport=transport, follow_redirects=False, timeout=timeout_seconds, headers=headers) as client:
        redirect_count = 0

        while redirect_count <= max_redirects:
            try:
                response = client.get(current_url)
            except httpx.HTTPError as e:
                raise ArticleExtractionError(f"HTTP request failed for '{current_url}': {e}")

            # Handle redirects manually to re-enforce SSRF validation on target location
            if response.is_redirect:
                redirect_count += 1
                if redirect_count > max_redirects:
                    raise ArticleExtractionError(f"Exceeded maximum redirect limit of {max_redirects}")

                location = response.headers.get("location")
                if not location:
                    raise ArticleExtractionError("Redirect response missing Location header")

                # Resolve relative redirect URLs
                current_url = str(response.url.join(location))
                validate_url_host(current_url)
                continue

            if response.status_code != 200:
                raise ArticleExtractionError(f"Server returned non-200 status code: {response.status_code}")

            content_type = response.headers.get("content-type", "").lower()
            if "text/html" not in content_type and "text/plain" not in content_type:
                raise ArticleExtractionError(f"Unsupported content type '{content_type}'. Expected HTML or plain text.")

            content_bytes = response.content
            if len(content_bytes) > max_bytes:
                raise ArticleExtractionError(f"Response size ({len(content_bytes)} bytes) exceeds limit of {max_bytes} bytes")

            break

    # Parse content
    html_text = content_bytes.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html_text, "html.parser")

    # Extract title
    title_tag = soup.find("title")
    title = title_tag.get_text().strip() if title_tag else "Extracted Article"

    # Clean non-content tags
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "noscript"]):
        tag.extract()

    # Prefer article tag or main container if present
    main_container = soup.find("article") or soup.find("main") or soup.find("body") or soup
    paragraphs = main_container.find_all(["p", "h1", "h2", "h3", "h4", "li"])

    extracted_lines = []
    for p in paragraphs:
        p_text = p.get_text().strip()
        if len(p_text) > 15:  # Filter out short menu items
            extracted_lines.append(p_text)

    full_text = "\n\n".join(extracted_lines)

    if len(full_text.strip()) < 100:
        raise ArticleExtractionError("Insufficient article text extracted from page (less than 100 characters).")

    canonical_url = current_url
    canonical_tag = soup.find("link", rel="canonical")
    if canonical_tag and canonical_tag.get("href"):
        canonical_url = canonical_tag["href"]

    return title, full_text, canonical_url
