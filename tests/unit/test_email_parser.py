from herald.db.models import RequestMode
from herald.extraction.email_parser import (
    SourceClassification,
    clean_email_text,
    compute_source_hash,
    extract_urls,
    html_to_text,
    process_email_message,
)


def test_clean_email_text_removes_signatures_and_replies():
    text = """Hello, please convert this email to a podcast.

-- 
John Doe
Software Engineer

On 2026-08-01 John wrote:
> Previous message body
"""
    cleaned = clean_email_text(text)
    assert "Hello, please convert this email to a podcast." in cleaned
    assert "John Doe" not in cleaned
    assert "Previous message body" not in cleaned


def test_html_to_text_conversion():
    html = """<html><body>
    <header>Nav Header</header>
    <h1>Article Title</h1>
    <p>This is paragraph content.</p>
    <footer>Footer Links</footer>
    </body></html>"""
    text = html_to_text(html)
    assert "Article Title" in text
    assert "This is paragraph content." in text
    assert "Nav Header" not in text
    assert "Footer Links" not in text


def test_extract_urls():
    text = "Check out https://example.com/page1 and http://test.org/news."
    urls = extract_urls(text)
    assert len(urls) == 2
    assert "https://example.com/page1" in urls
    assert "http://test.org/news" in urls


def test_process_email_message_url_dominant():
    subject = "Podcast: Brief"
    body = "https://example.com/tech-article"
    result = process_email_message(subject, body_text=body)

    assert result.mode == RequestMode.BRIEF
    assert result.classification == SourceClassification.URL
    assert result.detected_url == "https://example.com/tech-article"


def test_compute_source_hash_idempotent():
    h1 = compute_source_hash("Sample content", "https://example.com")
    h2 = compute_source_hash("Sample content", "https://example.com")
    h3 = compute_source_hash("Different content", "https://example.com")

    assert h1 == h2
    assert h1 != h3
