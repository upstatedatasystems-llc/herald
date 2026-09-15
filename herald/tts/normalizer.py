"""Spoken-Text Normalization Module for Herald TTS.

Provides deterministic conversion from canonical script narration to TTS-safe
spoken narration for Kokoro. Never alters canonical scripts in storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from herald.tts.lexicon import PronunciationLexicon, load_lexicon


@dataclass
class TransformationRecord:
    """Record of a single normalization transformation."""

    original: str
    spoken: str
    rule: str

    def to_dict(self) -> dict[str, str]:
        return {
            "original": self.original,
            "spoken": self.spoken,
            "rule": self.rule,
        }


@dataclass
class NormalizationResult:
    """Result of spoken text normalization with diagnostic traces."""

    spoken_text: str
    canonical_text: str
    transformations: list[TransformationRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"
]
TENS = [
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"
]


def int_to_words(n: int) -> str:
    """Convert an integer between 0 and 999 into spoken words."""
    if n < 0:
        return f"negative {int_to_words(-n)}"
    if n < 20:
        return ONES[n]
    if n < 100:
        tens_part = TENS[n // 10]
        rem = n % 10
        return f"{tens_part}-{ONES[rem]}" if rem else tens_part
    if n < 1000:
        hundred_part = f"{ONES[n // 100]} hundred"
        rem = n % 100
        return f"{hundred_part} {int_to_words(rem)}" if rem else hundred_part
    return str(n)


def year_to_words(year: int) -> str:
    """Convert a 4-digit year (1900-2099) into spoken words."""
    if not (1900 <= year <= 2099):
        return str(year)

    first_two = year // 100
    last_two = year % 100

    if year == 2000:
        return "two thousand"
    if 2001 <= year <= 2009:
        return f"two thousand {ONES[last_two]}"
    if last_two == 0:
        return f"{int_to_words(first_two)} hundred"
    if last_two < 10:
        return f"{int_to_words(first_two)} oh-{ONES[last_two]}"
    return f"{int_to_words(first_two)} {int_to_words(last_two)}"


def decade_to_words(decade_year: int) -> str:
    """Convert decade start year (e.g. 1990) into spoken decade (e.g. nineteen nineties)."""
    first_two = decade_year // 100
    tens_digit = (decade_year % 100) // 10
    tens_name = TENS[tens_digit]
    if tens_name.endswith("y"):
        plural_tens = tens_name[:-1] + "ies"
    else:
        plural_tens = tens_name + "s"

    if decade_year == 2000:
        return "two thousands"
    return f"{int_to_words(first_two)} {plural_tens}"


def normalize_for_speech(
    text: str,
    lexicon: PronunciationLexicon | None = None,
) -> NormalizationResult:
    """Deterministically convert canonical narration to TTS-safe spoken text.

    Pipeline Order:
    1. Unicode & typographic normalization
    2. Pronunciation lexicon (possessive-aware and exact matching)
    3. Letter-number identifiers (e.g. B-52, F-16)
    4. Decades & years
    5. Currency, percentages, and units
    6. Safe whitespace cleanup
    """
    if not text:
        return NormalizationResult(spoken_text="", canonical_text="", transformations=[], warnings=[])

    if lexicon is None:
        lexicon = load_lexicon()

    working = text
    transformations: list[TransformationRecord] = []
    warnings: list[str] = list(lexicon.load_warnings)

    # 1. Unicode & typographic normalization
    typo_replacements = [
        ("“", '"', "smart_quote"),
        ("”", '"', "smart_quote"),
        ("‘", "'", "smart_apostrophe"),
        ("’", "'", "smart_apostrophe"),
        ("—", " - ", "em_dash"),
        ("–", " - ", "en_dash"),
        ("…", "...", "ellipsis"),
    ]
    for orig, rep, rule in typo_replacements:
        if orig in working:
            working = working.replace(orig, rep)
            transformations.append(TransformationRecord(original=orig, spoken=rep, rule=rule))

    # 2. Pronunciation lexicon (user overrides + built-ins)
    mapping = lexicon.get_effective_mapping()

    # Sort tokens by length descending so longer tokens take precedence (e.g. JADES-GS-z14-0 before z14)
    sorted_tokens = sorted(mapping.keys(), key=len, reverse=True)

    for token in sorted_tokens:
        spoken = mapping[token]
        # Check possessives first (e.g., JWST's -> J W S T's)
        possessive_pattern = re.compile(rf"\b{re.escape(token)}'s\b")
        if possessive_pattern.search(working):
            possessive_spoken = f"{spoken}'s"
            working = possessive_pattern.sub(possessive_spoken, working)
            transformations.append(
                TransformationRecord(
                    original=f"{token}'s",
                    spoken=possessive_spoken,
                    rule="lexicon_possessive",
                )
            )

        # Exact token match
        if re.search(r"\W$", token):
            exact_pattern = re.compile(rf"\b{re.escape(token)}(?!\w)")
        else:
            exact_pattern = re.compile(rf"\b{re.escape(token)}\b")

        if exact_pattern.search(working):
            # Only record transformation if spoken differs from token
            if spoken != token:
                working = exact_pattern.sub(spoken, working)
                transformations.append(
                    TransformationRecord(
                        original=token,
                        spoken=spoken,
                        rule="lexicon_entry",
                    )
                )

    # 3. Letter-number identifiers (e.g. B-52 -> B fifty-two, F-16 -> F sixteen, 64-bit -> sixty-four bit)
    def _replace_letter_hyphen_number(match: re.Match) -> str:
        letter = match.group(1)
        num_str = match.group(2)
        try:
            num = int(num_str)
            if num < 100:
                words = int_to_words(num)
                orig = match.group(0)
                rep = f"{letter} {words}"
                transformations.append(
                    TransformationRecord(original=orig, spoken=rep, rule="letter_number_identifier")
                )
                return rep
        except ValueError:
            pass
        return match.group(0)

    working = re.sub(r"\b([A-Z])-(\d{1,3})\b", _replace_letter_hyphen_number, working)

    def _replace_bit_architecture(match: re.Match) -> str:
        bits = match.group(1)
        orig = match.group(0)
        try:
            val = int(bits)
            words = int_to_words(val)
            rep = f"{words}-bit"
            transformations.append(
                TransformationRecord(original=orig, spoken=rep, rule="bit_architecture")
            )
            return rep
        except ValueError:
            return orig

    working = re.sub(r"\b(8|16|32|64|128)-bit\b", _replace_bit_architecture, working)

    # 4. Decades & Years
    # Decades: 1990s, 1980s, 2020s, 1990's
    def _replace_decade(match: re.Match) -> str:
        orig = match.group(0)
        decade_val = int(match.group(1))
        spoken = decade_to_words(decade_val)
        transformations.append(
            TransformationRecord(original=orig, spoken=spoken, rule="decade")
        )
        return spoken

    working = re.sub(r"\b(19\d0|20\d0)'?s\b", _replace_decade, working)

    # Years: 1900-2099
    # Guarded against version numbers (v1.2026), IP addresses, serial codes, ports, or arbitrary numbers
    def _replace_year(match: re.Match) -> str:
        prefix = match.group(1) or ""
        year_str = match.group(2)
        suffix = match.group(3) or ""
        orig = year_str
        try:
            yr = int(year_str)
            # Guard against port numbers or software identifiers
            if prefix.lower().strip() in ("port", "model", "rfc", "iso", "ieee", "v", "version"):
                return match.group(0)
            spoken_yr = year_to_words(yr)
            transformations.append(
                TransformationRecord(original=orig, spoken=spoken_yr, rule="year")
            )
            return f"{prefix}{spoken_yr}{suffix}"
        except ValueError:
            return match.group(0)

    # Match year with contextual lookaround or common prepositions
    year_pattern = re.compile(
        r"(\b(?:in|since|by|from|to|until|through|year|during|around|before|after|between|[A-Z][a-z]+)\s+)"
        r"(19\d{2}|20\d{2})"
        r"(['\"”’]?[,\.]?(?:\s+|$))"
    )
    working = year_pattern.sub(_replace_year, working)

    # Also match standalone year at beginning of sentence or followed by comma / punctuation: "1955, and..."
    standalone_year_pattern = re.compile(
        r"(^|(?<=[.!?]\s)|(?<=[(]))"
        r"(19\d{2}|20\d{2})"
        r"(?=[,\.\)]|\s+(?:and|was|marked|saw|brought|became|began|ended))"
    )
    def _replace_standalone_year(match: re.Match) -> str:
        lead = match.group(1)
        year_str = match.group(2)
        yr = int(year_str)
        spoken_yr = year_to_words(yr)
        transformations.append(
            TransformationRecord(original=year_str, spoken=spoken_yr, rule="year")
        )
        return f"{lead}{spoken_yr}"

    working = standalone_year_pattern.sub(_replace_standalone_year, working)

    # 5. Currency, Percentages, Units
    # Currency: $3 billion, $250 million, $50, $1.50
    def _replace_currency(match: re.Match) -> str:
        symbol = match.group(1)
        amount = match.group(2)
        scale = match.group(3)
        orig = match.group(0)

        currency_name = "dollars" if symbol == "$" else ("euros" if symbol == "€" else "pounds")

        if scale:
            rep = f"{amount} {scale} {currency_name}"
        else:
            if "." in amount:
                parts = amount.split(".")
                whole = parts[0]
                cents = parts[1]
                if whole == "1":
                    unit_str = "dollar" if symbol == "$" else ("euro" if symbol == "€" else "pound")
                else:
                    unit_str = currency_name
                if whole == "0":
                    rep = f"{cents} cents"
                else:
                    rep = f"{whole} {unit_str} and {cents} cents"
            else:
                rep = f"{amount} {currency_name}"

        transformations.append(
            TransformationRecord(original=orig, spoken=rep, rule="currency")
        )
        return rep

    working = re.sub(
        r"([\$€£])(\d+(?:\.\d+)?)\s*(trillion|billion|million|thousand)?\b",
        _replace_currency,
        working,
    )

    # Percentages: 1.5%, 25 %
    def _replace_percentage(match: re.Match) -> str:
        num = match.group(1)
        orig = match.group(0)
        rep = f"{num} percent"
        transformations.append(
            TransformationRecord(original=orig, spoken=rep, rule="percentage")
        )
        return rep

    working = re.sub(r"(\d+(?:\.\d+)?)\s*%", _replace_percentage, working)

    # Units: GHz, MHz, GB, MB, km, kg, ms
    unit_map = {
        "GHz": "gigahertz",
        "MHz": "megahertz",
        "kHz": "kilohertz",
        "THz": "terahertz",
        "GB": "gigabytes",
        "MB": "megabytes",
        "KB": "kilobytes",
        "TB": "terabytes",
        "PB": "petabytes",
        "km": "kilometers",
        "kg": "kilograms",
        "ms": "milliseconds",
        "cm": "centimeters",
        "mm": "millimeters",
    }
    unit_pattern = re.compile(
        rf"\b(\d+(?:\.\d+)?)\s*({'|'.join(re.escape(u) for u in unit_map.keys())})\b"
    )

    def _replace_unit(match: re.Match) -> str:
        val = match.group(1)
        u = match.group(2)
        spoken_unit = unit_map[u]
        orig = match.group(0)
        rep = f"{val} {spoken_unit}"
        transformations.append(
            TransformationRecord(original=orig, spoken=rep, rule="unit")
        )
        return rep

    working = unit_pattern.sub(_replace_unit, working)

    # 6. Normalize excess whitespace
    working = re.sub(r"[ \t]+", " ", working).strip()

    return NormalizationResult(
        spoken_text=working,
        canonical_text=text,
        transformations=transformations,
        warnings=warnings,
    )
