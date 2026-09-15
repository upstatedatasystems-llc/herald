"""Pronunciation Lexicon for Herald TTS.

Provides built-in pronunciation mappings for technical identifiers, astronomy,
acronyms, defense systems, and hardware tokens, with safe optional user-extensible
dictionary overrides (JSON/YAML).
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("herald.tts.lexicon")

DEFAULT_LEXICON: dict[str, str] = {
    # Product protection (Herald stays Herald by default unless overridden)
    "Herald": "Herald",
    # Astronomical / Physical identifiers
    "JWST": "J W S T",
    "LIGO": "LIGO",  # Already phonetically pronounceable ("Lie-go")
    "ALMA": "ALMA",  # Already phonetically pronounceable ("Al-ma")
    "M87*": "M eighty-seven star",
    "MoM-z14": "M o M z fourteen",
    "JADES-GS-z14-0": "J A D E S G S z fourteen zero",
    "GW150914": "G W fifteen-oh-nine-fourteen",
    # Organizations / Common Acronyms
    "USDA": "U S D A",
    "TXRH": "T X R H",
    "FRISC": "FRISC",
    "DRAM": "DRAM",
    "YMTC": "Y M T C",
    # Defense / Aircraft
    "B-52": "B fifty-two",
    "F-16": "F sixteen",
    "A-10": "A ten",
    "U-2": "U two",
    # AI / Benchmark identifiers
    "GPT-4o": "G P T four o",
    "ARC-AGI-2": "A R C A G I two",
    # Hardware architectures / Memory
    "HBM3E": "H B M three E",
    "GDDR6X": "G D D R six X",
    "LPCAMM2": "L P C A M M two",
    "64-bit": "sixty-four bit",
    "32-bit": "thirty-two bit",
}


@dataclass
class PronunciationLexicon:
    """Manages built-in and user-defined pronunciation mappings."""

    overrides: dict[str, str] = field(default_factory=dict)
    load_warnings: list[str] = field(default_factory=list)
    custom_path: str | None = None

    def get_effective_mapping(self) -> dict[str, str]:
        """Combine built-in lexicon with user overrides (overrides win)."""
        combined = dict(DEFAULT_LEXICON)
        combined.update(self.overrides)
        return combined

    def lookup(self, token: str) -> str | None:
        """Lookup token in combined lexicon."""
        return self.get_effective_mapping().get(token)


def load_lexicon(path: str | Path | None = None) -> PronunciationLexicon:
    """Load pronunciation lexicon from file path if specified, falling back to built-ins.

    Supports JSON and YAML formats. Malformed files log a warning and fall back
    cleanly without breaking runtime.
    """
    if not path:
        return PronunciationLexicon()

    p = Path(path)
    if not p.exists() or not p.is_file():
        logger.debug(f"Pronunciation lexicon file not found at '{path}'; using built-ins.")
        return PronunciationLexicon(custom_path=str(path))

    overrides: dict[str, str] = {}
    load_warnings: list[str] = []

    try:
        content = p.read_text(encoding="utf-8")
        raw_data: Any = None

        if p.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml

                raw_data = yaml.safe_load(content)
            except Exception as ye:
                msg = f"Failed to parse YAML pronunciation lexicon at '{p}': {ye}"
                logger.warning(msg)
                load_warnings.append(msg)
        else:
            try:
                raw_data = json.loads(content)
            except Exception as je:
                msg = f"Failed to parse JSON pronunciation lexicon at '{p}': {je}"
                logger.warning(msg)
                load_warnings.append(msg)

        if isinstance(raw_data, dict):
            for k, v in raw_data.items():
                if isinstance(v, str):
                    overrides[str(k).strip()] = v.strip()
                elif isinstance(v, dict) and "spoken" in v:
                    overrides[str(k).strip()] = str(v["spoken"]).strip()
        elif raw_data is not None:
            msg = f"Lexicon at '{p}' must be a mapping/dictionary, got {type(raw_data).__name__}"
            logger.warning(msg)
            load_warnings.append(msg)

    except Exception as e:
        msg = f"Unexpected error loading pronunciation lexicon from '{p}': {e}"
        logger.warning(msg)
        load_warnings.append(msg)

    return PronunciationLexicon(overrides=overrides, load_warnings=load_warnings, custom_path=str(path))
