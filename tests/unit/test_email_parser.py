from packages.herald.db.models import RequestMode
from packages.herald.extraction.email_parser import (
    clean_email_text,
    compute_source_hash,
    extract_urls,
    html_to_text,
    process_email_message,
)


def test_clean_email_text_removes_signatures_and_replies():
    raw_email = """Here is the main newsletter content for today.

Important announcements and discussion.

On Thu, Aug 6, 2026 at 10:00 AM user@example.com wrote:
> Quoted reply thread text should be removed.
--
Signature text
Unsubscribe from newsletter
"""
    cleaned = clean_email_text(raw_email)
    assert "main newsletter content" in cleaned
    assert "Important announcements" in cleaned
    assert "Quoted reply thread" not in cleaned
    assert "Signature text" not in cleaned


def test_html_to_text_conversion():
    html_content = """<html>
    <head><style>body { color: red; }</style></head>
    <body>
        <h1>Headline Title</h1>
        <p>This is paragraph text.</p>
        <script>alert('bad');</script>
    </body>
    </html>"""
    text = html_to_text(html_content)
    assert "Headline Title" in text
    assert "This is paragraph text." in text
    assert "alert" not in text
    assert "color: red" not in text


def test_extract_urls():
    text = "Check out https://example.com/article?id=123 for details and http://test.org."
    urls = extract_urls(text)
    assert "https://example.com/article?id=123" in urls
    assert "http://test.org" in urls


def test_process_email_message_url_dominant():
    res = process_email_message(
        subject="Podcast: Brief",
        body_text="Here is a great article: https://example.com/news/123",
    )
    assert res.mode == RequestMode.BRIEF
    assert res.detected_url == "https://example.com/news/123"
    assert res.is_url_dominant is True


def test_compute_source_hash_idempotent():
    h1 = compute_source_hash("Sample source text", "https://example.com/article")
    h2 = compute_source_hash("Sample source text", "https://example.com/article")
    h3 = compute_source_hash("Different source text", "https://example.com/article")

    assert h1 == h2
    assert h1 != h3
