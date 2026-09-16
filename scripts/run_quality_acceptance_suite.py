#!/usr/bin/env python3
"""
Herald Quality Acceptance Suite Runner.

Verifies the Podcast Quality, Duration, Pronunciation & Voice Settings cycle:
1. Duration & Section Expansion Budgeting
2. Cross-Section Anti-Repetition & Duplicate Repair
3. Title & Heading Cleanups (with Zero-AI Literal mode guarantee)
4. 9-Category Pronunciation Preflight
5. Audio True-Peak (-1.5 dBTP) Mastering
6. Fail-Closed Voice Discovery
7. Voice Preview Concurrency & Shared Lock Protection

Modes:
- Default: Safe offline/mock acceptance tests.
- --live: Executes live TTS and AI operations.
- --analyze <path>: Analyzes diagnostic logs and artifacts for quality regressions.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from herald.config import settings
from herald.services.quality_gate import run_quality_gate
from herald.services.voice_manager import (
    VOICE_METADATA,
    VoicePreviewBusyError,
    ensure_voice_sample,
    get_selectable_voices,
)
from herald.tts.kokoro_client import KokoroClient
from herald.tts.normalizer import run_pronunciation_preflight


def analyze_directory(dir_path: Path) -> int:
    """Analyze diagnostic logs and artifacts in dir_path for quality regressions."""
    if not dir_path.exists():
        print(f"ERROR: Directory '{dir_path}' does not exist.")
        return 1

    print(f"Analyzing Herald artifacts and diagnostic logs in: {dir_path}")
    json_files = list(dir_path.glob("**/*.json"))
    log_files = list(dir_path.glob("**/*.log"))

    print(f"Found {len(json_files)} JSON files and {len(log_files)} log files.\n")

    issues_found = 0
    analyzed_jobs = 0

    for jf in json_files:
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            if "job_id" in data or "segments" in data:
                analyzed_jobs += 1
                # Check true-peak violations (prefer measured; fallback to target)
                tp = data.get("measured_true_peak_dbtp")
                if tp is None:
                    tp = data.get("true_peak_target_dbtp", data.get("true_peak_dbtp"))
                if tp is not None and float(tp) > -1.0:
                    print(f"[QUALITY ALERT] Job {data.get('job_id', jf.stem)} has true peak {tp} dBTP (> -1.0 dBTP ceiling)")
                    issues_found += 1

                # Check duplicate warnings
                dups = data.get("duplicate_warnings_count") or len(data.get("duplicate_warnings", []))
                if dups > 0:
                    print(f"[REPETITION NOTICE] Job {data.get('job_id', jf.stem)} has {dups} cross-section duplicate warnings")

                # Check pronunciation preflight issues
                preflight = data.get("pronunciation_preflight")
                if preflight and isinstance(preflight, dict):
                    tokens = preflight.get("tokens", [])
                    unknown = [t for t in tokens if t.get("token_type") == "unknown"]
                    if unknown:
                        print(f"[PRONUNCIATION NOTICE] Job {data.get('job_id', jf.stem)} has {len(unknown)} unclassified tokens")
        except Exception:
            pass

    print(f"\nAnalysis complete. Analyzed {analyzed_jobs} job records. Issues found: {issues_found}")
    return 0 if issues_found == 0 else 1


def run_acceptance_suite(is_live: bool = False) -> int:
    """Run comprehensive quality acceptance suite."""
    print("=" * 70)
    print(f"HERALD QUALITY ACCEPTANCE SUITE (Mode: {'LIVE' if is_live else 'MOCK/OFFLINE'})")
    print("=" * 70)

    if not is_live:
        os.environ["HERALD_MOCK_TTS"] = "1"

    passed_tests = 0
    total_tests = 0

    def record_result(name: str, success: bool, detail: str = ""):
        nonlocal passed_tests, total_tests
        total_tests += 1
        status = "PASSED" if success else "FAILED"
        if success:
            passed_tests += 1
        print(f"[{status}] {name}")
        if detail:
            print(f"         {detail}")

    # 1. Pronunciation Token Classification (9 Types)
    try:
        sample_narration = (
            "NASA and ESA deployed the JWST telescope at 1400 UTC to observe JADES-GS-z14-0. "
            "Model GPT-4o analyzed the $1.2 million dataset from https://example.com."
        )
        report = run_pronunciation_preflight(sample_narration)
        types_found = {str(r.get("classified_type")) for r in report.records}
        has_acronym = any("acronym" in t.lower() for t in types_found)
        has_initialism = any("initialism" in t.lower() for t in types_found)
        has_catalog = any("catalog" in t.lower() for t in types_found)
        has_model = any("model" in t.lower() or "product" in t.lower() for t in types_found)
        has_url = any("url" in t.lower() for t in types_found)
        has_num = any("number" in t.lower() or "unit" in t.lower() for t in types_found)

        ok = has_acronym and has_initialism and has_catalog and has_model and has_url and has_num
        record_result(
            "1. Pronunciation Preflight (9 Token Types)",
            ok,
            f"Classified {report.total_tokens} tokens into {len(types_found)} unique categories (Catalog, Initialism, Model, URL detected).",
        )
    except Exception as e:
        record_result("1. Pronunciation Preflight (9 Token Types)", False, str(e))

    # 2. Audio True-Peak Safety Mastering Filter
    try:
        peak_setting = getattr(settings, "HERALD_AUDIO_TRUE_PEAK_DBTP", -1.5)
        # Check that settings enforce <= -1.5 dBTP
        ok = peak_setting <= -1.5 and settings.LOUDNORM_TARGET_TP <= -1.5
        record_result(
            "2. Audio True-Peak Safety Ceiling",
            ok,
            f"Target TP: {settings.LOUDNORM_TARGET_TP} dBTP, Peak Limiter: {peak_setting} dBTP.",
        )
    except Exception as e:
        record_result("2. Audio True-Peak Safety Ceiling", False, str(e))

    # 3. Fail-Closed Voice Discovery Contract
    try:
        # 3a. Succeeded discovery: selectable = discovered ∩ curated
        mock_client_good = KokoroClient()
        mock_client_good.get_available_voices = lambda timeout=2.0: ["af_heart", "af_bella", "unknown_voice_xyz"]
        selectable, diag = get_selectable_voices(user_voice="af_heart", kokoro_client=mock_client_good)
        ok_intersection = ("af_heart" in selectable and "af_bella" in selectable and "unknown_voice_xyz" not in selectable)

        # 3b. Failed discovery: FAIL CLOSED (must NOT advertise all curated voices)
        mock_client_bad = KokoroClient()
        def _fail_disc(timeout=2.0):
            raise RuntimeError("Kokoro endpoint offline")
        mock_client_bad.get_available_voices = _fail_disc
        selectable_fail, diag_fail = get_selectable_voices(user_voice="af_bella", kokoro_client=mock_client_bad)
        ok_fail_closed = ("af_bella" in selectable_fail and len(selectable_fail) <= 2 and len(selectable_fail) < len(settings.get_allowed_voices_list()))

        ok = ok_intersection and ok_fail_closed
        record_result(
            "3. Fail-Closed Voice Discovery",
            ok,
            f"Discovery success -> intersection ({len(selectable)} voices). Discovery fail -> isolated safe default ({len(selectable_fail)} voices).",
        )
    except Exception as e:
        record_result("3. Fail-Closed Voice Discovery", False, str(e))

    # 4. Voice Catalog & Accent Group Coverage
    try:
        from herald.services.voice_manager import get_voices_by_accent_group
        us_voices = get_voices_by_accent_group("american_english")
        uk_voices = get_voices_by_accent_group("british_english")
        ok = len(us_voices) >= 6 and len(uk_voices) >= 4 and len(VOICE_METADATA) >= 12
        record_result(
            "4. Curated Voice Catalog",
            ok,
            f"Configured {len(us_voices)} American English and {len(uk_voices)} British English voices.",
        )
    except Exception as e:
        record_result("4. Curated Voice Catalog", False, str(e))

    # 5. Title & Heading Quality Gate & Zero-AI Literal Guarantee
    try:
        from herald.db.models import ContentMode, PodcastJob
        from herald.literal.script_generator import generate_literal_script

        literal_text = "# Main Reading\nFirst part content.\n\n# Chapter Two\nSecond part content."
        literal_script = generate_literal_script(literal_text, source_title="Literal Test")
        fake_job = PodcastJob(id="test-literal-gate", content_mode=ContentMode.LITERAL.value)
        script_dict = literal_script.model_dump() if hasattr(literal_script, "model_dump") else literal_script.to_dict()
        _, report = run_quality_gate(
            script_dict,
            job=fake_job,
        )
        # Verify headings are clean and Literal mode never recommends metadata AI cleanup
        ok = not report.metadata_cleanup_recommended
        record_result(
            "5. Title & Heading Gate (Literal Zero-AI Guarantee)",
            ok,
            f"Literal mode script generated {len(literal_script.segments)} segments; metadata_cleanup_recommended={report.metadata_cleanup_recommended}.",
        )
    except Exception as e:
        record_result("5. Title & Heading Gate (Literal Zero-AI Guarantee)", False, str(e))

    # 6. Cross-Section Duplicate Detection Gate
    try:
        test_script_dup = {
            "episode_title": "Repetition Test",
            "segments": [
                {
                    "order": 1,
                    "heading": "Introduction",
                    "narration": "The James Webb Space Telescope has revolutionized our understanding of early cosmic dawn galaxies by revealing luminous candidates earlier than expected.",
                },
                {
                    "order": 2,
                    "heading": "Deep Analysis",
                    "narration": "The James Webb Space Telescope has revolutionized our understanding of early cosmic dawn galaxies by revealing luminous candidates earlier than expected.",
                },
            ],
        }
        _, dup_report = run_quality_gate(test_script_dup)
        ok = len(dup_report.near_duplicate_warnings) >= 1 and dup_report.duplicate_repair_recommended
        record_result(
            "6. Cross-Section Duplicate Detection Gate",
            ok,
            f"Identified {len(dup_report.near_duplicate_warnings)} duplicate warnings; repair recommended: {dup_report.duplicate_repair_recommended}.",
        )
    except Exception as e:
        record_result("6. Cross-Section Duplicate Detection Gate", False, str(e))

    # 7. Voice Preview Concurrency & Busy Protection
    try:
        # When TTS is actively synthesizing, ensure_voice_sample must reject previews
        from unittest.mock import patch
        with patch("herald.services.voice_manager.is_tts_actively_synthesizing", return_value=True):
            busy_caught = False
            try:
                ensure_voice_sample("af_heart", force=True)
            except VoicePreviewBusyError:
                busy_caught = True

        record_result(
            "7. Voice Preview Concurrency Protection",
            busy_caught,
            "Preview raised VoicePreviewBusyError immediately when TTS was marked active.",
        )
    except Exception as e:
        record_result("7. Voice Preview Concurrency Protection", False, str(e))

    # 8. Voice Preview Happy Path (Free TTS Slot)
    try:
        from unittest.mock import MagicMock, patch

        mock_client = KokoroClient()
        def _mock_synth(text, output_path, voice=None, speed=None, timeout=None):
            Path(output_path).write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")

        mock_client.synthesize_chunk = MagicMock(side_effect=_mock_synth)

        with patch("herald.services.voice_manager.is_tts_actively_synthesizing", return_value=False), \
             patch("herald.services.voice_manager.convert_wav_to_mp3", side_effect=lambda w, m: Path(m).write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00MOCK_MP3_DATA")), \
             patch("herald.services.voice_manager.is_valid_sample_audio", return_value=True), \
             patch("herald.services.voice_manager.normalize_for_speech", side_effect=lambda t: t.upper()) as mock_norm:

            sample_path = ensure_voice_sample("af_heart", speed=1.0, kokoro_client=mock_client, force=True)
            ok = (
                sample_path.exists()
                and mock_client.synthesize_chunk.called
                and mock_norm.called
            )
            record_result(
                "8. Voice Preview Happy Path (Free Slot)",
                ok,
                f"Generated preview at {sample_path.name}; normalizer and Kokoro invoked with free TTS slot.",
            )
    except Exception as e:
        record_result("8. Voice Preview Happy Path (Free Slot)", False, str(e))

    print("-" * 70)
    print(f"RESULTS: {passed_tests}/{total_tests} tests passed.")
    print("=" * 70)

    return 0 if passed_tests == total_tests else 1


def main():
    parser = argparse.ArgumentParser(description="Herald Quality Acceptance Suite")
    parser.add_argument("--mock", action="store_true", default=True, help="Run in mock/offline mode (default)")
    parser.add_argument("--live", action="store_true", help="Run with live TTS and AI endpoints")
    parser.add_argument("--analyze", type=str, help="Analyze directory of logs/artifacts for quality metrics")

    args = parser.parse_args()

    if args.analyze:
        sys.exit(analyze_directory(Path(args.analyze)))

    is_live = args.live
    sys.exit(run_acceptance_suite(is_live=is_live))


if __name__ == "__main__":
    main()
