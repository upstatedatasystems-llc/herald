"""Unit tests for pronunciation classification and preflight inspection.

Tests:
1. Token classification across 9 categories.
2. Regression coverage for catalog identifiers: B-52, MoM-z14, JADES-GS-z14-0, GPT-4o.
3. Pronunciation preflight report structure, counts, and origin labeling.
4. Boundedness of preflight diagnostic records (capped at 50).
"""

from herald.tts.lexicon import load_lexicon
from herald.tts.normalizer import (
    TokenType,
    classify_token,
    run_pronunciation_preflight,
)


def test_classify_lexicon_and_catalog_tokens():
    lex = load_lexicon()

    # B-52 defense/model identifier
    c_b52 = classify_token("B-52", lexicon=lex)
    assert c_b52.token_type == TokenType.PRODUCT_MODEL_IDENTIFIER
    assert c_b52.origin == "lexicon"
    assert c_b52.spoken == "B fifty-two"

    # MoM-z14 scientific catalog identifier
    c_mom = classify_token("MoM-z14", lexicon=lex)
    assert c_mom.token_type == TokenType.SCIENTIFIC_CATALOG_IDENTIFIER
    assert c_mom.origin == "lexicon"
    assert "M o M z fourteen" in c_mom.spoken

    # JADES-GS-z14-0 scientific catalog identifier
    c_jades = classify_token("JADES-GS-z14-0", lexicon=lex)
    assert c_jades.token_type == TokenType.SCIENTIFIC_CATALOG_IDENTIFIER
    assert c_jades.origin == "lexicon"

    # GPT-4o product model identifier
    c_gpt = classify_token("GPT-4o", lexicon=lex)
    assert c_gpt.token_type == TokenType.PRODUCT_MODEL_IDENTIFIER
    assert c_gpt.origin == "lexicon"


def test_classify_acronyms_and_initialisms():
    lex = load_lexicon()

    # Acronyms pronounced as words
    for acr in ["NASA", "NATO", "ALMA", "LIGO"]:
        c = classify_token(acr, lexicon=lex)
        assert c.token_type == TokenType.ACRONYM, f"Failed for {acr}"

    # Initialisms spoken letter-by-letter
    for init in ["JWST", "USDA", "FBI", "CIA", "MIT"]:
        c = classify_token(init, lexicon=lex)
        assert c.token_type == TokenType.INITIALISM, f"Failed for {init}"


def test_classify_numbers_currency_units_urls():
    lex = load_lexicon()

    # Currency
    c_curr = classify_token("$300", lexicon=lex)
    assert c_curr.token_type == TokenType.NUMBER_DATE_UNIT

    # Percentages
    c_pct = classify_token("25%", lexicon=lex)
    assert c_pct.token_type == TokenType.NUMBER_DATE_UNIT

    # Units
    c_unit = classify_token("5GB", lexicon=lex)
    assert c_unit.token_type == TokenType.NUMBER_DATE_UNIT

    # URLs
    c_url = classify_token("https://example.com/data", lexicon=lex)
    assert c_url.token_type == TokenType.URL_DOMAIN


def test_classify_normal_words():
    lex = load_lexicon()
    for word in ["podcast", "narrative", "universe", "telescope", "quality"]:
        c = classify_token(word, lexicon=lex)
        assert c.token_type == TokenType.NORMAL_WORD
        assert c.origin == "standard"


def test_run_pronunciation_preflight_report():
    sample_text = (
        "In 1955, NASA deployed the B-52 while JWST observed MoM-z14 and JADES-GS-z14-0. "
        "The $3 billion program evaluated GPT-4o on 64-bit chips at 3 GHz."
    )
    report = run_pronunciation_preflight(sample_text)

    assert report.total_tokens > 15
    assert report.unusual_token_count > 0
    assert report.lexicon_hits > 0
    assert len(report.records) == report.unusual_token_count

    # Check specific tokens in records
    found_tokens = {r["token"] for r in report.records}
    assert "B-52" in found_tokens
    assert "MoM-z14" in found_tokens
    assert "JADES-GS-z14-0" in found_tokens
    assert "GPT-4o" in found_tokens

    # Verify origin labels
    b52_record = next(r for r in report.records if r["token"] == "B-52")
    assert b52_record["origin"] == "lexicon"


def test_pronunciation_preflight_boundedness():
    # Long text with more than 60 unusual tokens
    tokens = [f"Token-{i:03d}" for i in range(100)]
    long_text = " ".join(tokens)
    report = run_pronunciation_preflight(long_text)

    # Records must be capped at 50 to protect diagnostics size
    assert len(report.records) <= 50
