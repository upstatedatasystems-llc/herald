from herald.extraction.source_cleaner import clean_source_text, sanitize_unicode


def test_unicode_preservation_fixtures():
    """Verify that Pločnik, Vinča, and Çatalhöyük survive NFC normalization and control character stripping."""
    sample = "Archaeological sites at Pločnik, Vinča, and Çatalhöyük represent significant Neolithic settlements.\x07\x08"

    clean_unicode = sanitize_unicode(sample)
    assert "Pločnik" in clean_unicode
    assert "Vinča" in clean_unicode
    assert "Çatalhöyük" in clean_unicode
    # Verify control chars \x07 \x08 were stripped
    assert "\x07" not in clean_unicode
    assert "\x08" not in clean_unicode

    cleaned_text = clean_source_text(sample)
    assert "Pločnik" in cleaned_text
    assert "Vinča" in cleaned_text
    assert "Çatalhöyük" in cleaned_text
