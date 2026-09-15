"""Unit tests for Phase 2 Spoken-Text Normalization and Pronunciation Lexicon.

Tests:
A. Canonical preservation (canonical never mutated, deterministic repeatability)
B. Lexicon (built-in defaults, user overrides, malformed fallback, missing file safety)
C. Acronyms & possessives (JWST -> J W S T, JWST's -> J W S T's, pronounceable LIGO preserved)
D. Technical / Letter-number identifiers (B-52, F-16, GPT-4o, ARC-AGI-2, MoM-z14, JADES-GS-z14-0, GW150914, M87*, HBM3E, GDDR6X, LPCAMM2, 64-bit)
E. Numbers, Years, Decades, Money, Percentages, Units (1955, 2026, 1990s, $3 billion, 1.5%, 3 GHz, 5 GB)
F. Herald product name protection
G. Transformation trace diagnostics
"""

import json
from pathlib import Path

from herald.tts.lexicon import PronunciationLexicon, load_lexicon
from herald.tts.normalizer import normalize_for_speech


def test_canonical_preservation():
    canonical = "In 1955, the B-52 entered service, and JWST's observations changed astronomy."
    res1 = normalize_for_speech(canonical)
    res2 = normalize_for_speech(canonical)

    # Canonical is returned unchanged in result metadata
    assert res1.canonical_text == canonical
    assert res2.canonical_text == canonical

    # Normalization is strictly deterministic
    assert res1.spoken_text == res2.spoken_text
    assert [t.to_dict() for t in res1.transformations] == [t.to_dict() for t in res2.transformations]

    # Spoken text is modified for TTS readability
    assert "nineteen fifty-five" in res1.spoken_text
    assert "B fifty-two" in res1.spoken_text
    assert "J W S T's" in res1.spoken_text


def test_builtin_lexicon_and_herald_protection():
    lex = load_lexicon()
    assert lex.lookup("Herald") == "Herald"
    assert lex.lookup("JWST") == "J W S T"
    assert lex.lookup("LIGO") == "LIGO"

    res = normalize_for_speech("Welcome to Herald. Today we discuss the JWST mission.", lexicon=lex)
    assert "Welcome to Herald." in res.spoken_text
    assert "J W S T" in res.spoken_text


def test_user_lexicon_override_wins(tmp_path: Path):
    override_file = tmp_path / "custom_lexicon.json"
    override_file.write_text(
        json.dumps({
            "Herald": "Hair-uld",
            "JWST": "James Webb",
        }),
        encoding="utf-8",
    )

    lex = load_lexicon(override_file)
    assert lex.lookup("Herald") == "Hair-uld"
    assert lex.lookup("JWST") == "James Webb"
    # Unoverridden built-in remains active
    assert lex.lookup("B-52") == "B fifty-two"

    res = normalize_for_speech("Herald with JWST.", lexicon=lex)
    assert "Hair-uld" in res.spoken_text
    assert "James Webb" in res.spoken_text


def test_malformed_user_lexicon_fails_safely(tmp_path: Path):
    bad_file = tmp_path / "bad_lexicon.json"
    bad_file.write_text("{malformed json content: [[", encoding="utf-8")

    lex = load_lexicon(bad_file)
    assert len(lex.load_warnings) > 0
    # Still provides built-in defaults without crashing
    assert lex.lookup("JWST") == "J W S T"

    res = normalize_for_speech("Testing JWST.", lexicon=lex)
    assert "J W S T" in res.spoken_text
    assert len(res.warnings) > 0


def test_missing_lexicon_requires_no_setup():
    lex = load_lexicon("/non/existent/path/lexicon.json")
    assert lex.lookup("JWST") == "J W S T"


def test_acronyms_and_possessives():
    text = "JWST and JWST's primary mirror alongside USDA regulations and LIGO sensors."
    res = normalize_for_speech(text)

    # JWST letter-spaced
    assert "J W S T" in res.spoken_text
    # Possessive apostrophe-s remains attached, not broken into ' s
    assert "J W S T's" in res.spoken_text
    assert "J W S T ' s" not in res.spoken_text

    # USDA letter-spaced
    assert "U S D A" in res.spoken_text

    # LIGO is pronounceable acronym and remains intact
    assert "LIGO" in res.spoken_text
    assert "L I G O" not in res.spoken_text


def test_technical_identifiers():
    sample = (
        "We tested the B-52 and F-16 airframes with GPT-4o and ARC-AGI-2 models. "
        "Distant galaxies MoM-z14 and JADES-GS-z14-0 were detected after GW150914. "
        "Black hole M87* was examined with HBM3E, GDDR6X, and LPCAMM2 on 64-bit systems."
    )
    res = normalize_for_speech(sample)
    spoken = res.spoken_text

    assert "B fifty-two" in spoken
    assert "F sixteen" in spoken
    assert "G P T four o" in spoken
    assert "A R C A G I two" in spoken
    assert "M o M z fourteen" in spoken
    assert "J A D E S G S z fourteen zero" in spoken
    assert "G W fifteen-oh-nine-fourteen" in spoken
    assert "M eighty-seven star" in spoken
    assert "H B M three E" in spoken
    assert "G D D R six X" in spoken
    assert "L P C A M M two" in spoken
    assert "sixty-four bit" in spoken or "sixty-four-bit" in spoken or "64-bit" in spoken


def test_numbers_years_decades():
    text = "In 1955, the program started. By 2026, the 1990s era tech was replaced."
    res = normalize_for_speech(text)
    spoken = res.spoken_text

    assert "nineteen fifty-five" in spoken
    assert "twenty twenty-six" in spoken
    assert "nineteen nineties" in spoken


def test_year_guards_against_version_and_model_false_positives():
    text = "We configured port 2026 and model 1955 on version 1.2026."
    res = normalize_for_speech(text)
    spoken = res.spoken_text

    # Guarded against converting port numbers, model numbers, or dotted decimals to words
    assert "port 2026" in spoken
    assert "model 1955" in spoken


def test_currency_percentage_and_units():
    text = "The budget grew by 1.5% to $3 billion, while another $250 million funded a 3 GHz, 5 GB chip across 10 km."
    res = normalize_for_speech(text)
    spoken = res.spoken_text

    assert "1.5 percent" in spoken
    assert "3 billion dollars" in spoken
    assert "250 million dollars" in spoken
    assert "3 gigahertz" in spoken
    assert "5 gigabytes" in spoken
    assert "10 kilometers" in spoken


def test_transformation_trace():
    text = "In 1955, JWST observed $3 billion in assets."
    res = normalize_for_speech(text)

    rules = [t.rule for t in res.transformations]
    assert "year" in rules
    assert "lexicon_entry" in rules
    assert "currency" in rules

    traces = [t.to_dict() for t in res.transformations]
    assert any(t["original"] == "JWST" and t["spoken"] == "J W S T" for t in traces)
    assert any(t["original"] == "$3 billion" and t["spoken"] == "3 billion dollars" for t in traces)
