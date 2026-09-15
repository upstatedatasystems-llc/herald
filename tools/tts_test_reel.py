"""Manual Acceptance Test Reel for Herald Phase 2: Narration & Audio Quality.

Deterministic local utility that generates ~60-90 seconds of audio exercising:
- Product name: Herald (with explicit A/B candidate override support)
- Astronomy: JWST, JWST's, LIGO, MoM-z14, JADES-GS-z14-0, GW150914, M87*
- Defense / Models: B-52, F-16, GPT-4o, ARC-AGI-2
- Hardware: HBM3E, GDDR6X, LPCAMM2, 64-bit
- Numbers / Units: 1955, 2026, 1990s, 1.5%, $3 billion, $250 million, 3 GHz, 5 GB, 10 km
- Semantic Boundaries: Sentence, paragraph, section, and branding transitions.

Generates machine-readable diagnostics artifact:
- tts-chunks.json (intro, body chunks, and outro with canonical/spoken text, transformations, boundaries, silence)
- test-reel-summary.json (overall execution summary)

Fails closed if any chunk synthesis fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from herald.audio.branding import (
    render_intro_narration,
    render_outro_narration,
    synthesize_branding_segment,
)
from herald.audio.ffmpeg_builder import (
    inspect_pcm_wav_file,
    join_and_normalize_audio,
    measure_wav_silence,
    validate_audio_file,
)
from herald.audio.pause_policy import (
    PAUSE_BRANDING,
    BoundaryType,
)
from herald.config import settings
from herald.tts.chunker import chunk_podcast_script
from herald.tts.kokoro_client import KokoroClient
from herald.tts.lexicon import load_lexicon
from herald.tts.normalizer import normalize_for_speech

TEST_REEL_SEGMENTS = [
    {
        "order": 1,
        "heading": "Aerospace and Astronomy",
        "narration": (
            "In 1955, the B-52 entered service alongside the F-16 in modern fleets.\n\n"
            "Decades later, the JWST and JWST's primary mirror discovered galaxies MoM-z14 and JADES-GS-z14-0. "
            "Meanwhile, LIGO observed gravitational wave GW150914, while astronomers mapped black hole M87*."
        ),
    },
    {
        "order": 2,
        "heading": "Compute Architecture and Research Funding",
        "narration": (
            "By 2026, models like GPT-4o and ARC-AGI-2 tested the limits of modern intelligence.\n\n"
            "Hardware teams integrated HBM3E, GDDR6X, and LPCAMM2 memory into 64-bit platforms running at 3 GHz with 5 GB caches. "
            "Overall, investment grew by 1.5% to $3 billion, while an additional $250 million supported sensor networks across 10 km."
        ),
    },
]


def run_test_reel(
    output_dir: Path,
    dry_run: bool = False,
    voice: str = "af_heart",
    speed: float = 1.0,
    base_url: str | None = None,
    lexicon_path: str | Path | None = None,
    herald_override: str | None = None,
    allow_partial: bool = False,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = output_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    effective_url = base_url or settings.KOKORO_BASE_URL

    # 1. Pronunciation Lexicon & Candidate Overrides (A/B testing support)
    lex = load_lexicon(lexicon_path)
    if herald_override:
        lex.overrides["Herald"] = herald_override

    effective_herald_spoken = lex.lookup("Herald") or "Herald"

    print("=" * 80)
    print(" HERALD PHASE 2: NARRATION & AUDIO QUALITY TEST REEL")
    print("=" * 80)
    print(f"Output Directory:  {output_dir}")
    print(f"Voice:             {voice} | Speed: {speed} | Base URL: {effective_url}")
    print(f"Herald Spoken A/B: canonical 'Herald' -> spoken '{effective_herald_spoken}'")
    print(f"Execution Mode:    {'DRY RUN (Analysis Only)' if dry_run else 'LIVE SYNTHESIS'}\n")

    # 2. Canonical Scripts & Deterministic Spoken Normalization
    intro_canonical = render_intro_narration(
        episode_title="Narration and Audio Benchmark",
        publisher="BBC Sky at Night Magazine",
        target_minutes=1,
    )
    intro_norm = normalize_for_speech(intro_canonical, lexicon=lex)

    body_chunks = chunk_podcast_script(TEST_REEL_SEGMENTS, max_chars=400, lexicon=lex)

    outro_canonical = render_outro_narration()
    outro_norm = normalize_for_speech(outro_canonical, lexicon=lex)

    # 3. Build diagnostic chunk items
    diagnostic_items: list[dict[str, Any]] = []

    # Item 0: Intro Branding
    intro_diag: dict[str, Any] = {
        "index": 0,
        "segment_type": "INTRO",
        "canonical_text": intro_canonical,
        "spoken_text": intro_norm.spoken_text,
        "transformations": [t.to_dict() for t in intro_norm.transformations],
        "boundary_type": BoundaryType.BRANDING.value,
        "pause_duration_ms": int(round(PAUSE_BRANDING * 1000)),
        "voice": voice,
        "speed": speed,
        "wav_path": None,
        "audio_duration": None,
        "leading_silence_ms": None,
        "trailing_silence_ms": None,
        "status": "DRY_RUN" if dry_run else "PENDING",
        "error_detail": None,
    }
    diagnostic_items.append(intro_diag)

    # Items 1..N: Body Chunks
    for chk in body_chunks:
        chk_diag: dict[str, Any] = {
            "index": chk.index,
            "segment_type": "BODY",
            "canonical_text": chk.canonical_text,
            "spoken_text": chk.text,
            "transformations": chk.transformations,
            "boundary_type": chk.boundary_type.value,
            "pause_duration_ms": int(round(chk.pause_duration_seconds * 1000)),
            "voice": voice,
            "speed": speed,
            "wav_path": None,
            "audio_duration": None,
            "leading_silence_ms": None,
            "trailing_silence_ms": None,
            "status": "DRY_RUN" if dry_run else "PENDING",
            "error_detail": None,
        }
        diagnostic_items.append(chk_diag)

    # Item N+1: Outro Branding
    outro_idx = len(body_chunks) + 1
    outro_diag: dict[str, Any] = {
        "index": outro_idx,
        "segment_type": "OUTRO",
        "canonical_text": outro_canonical,
        "spoken_text": outro_norm.spoken_text,
        "transformations": [t.to_dict() for t in outro_norm.transformations],
        "boundary_type": BoundaryType.BRANDING.value,
        "pause_duration_ms": 0,
        "voice": voice,
        "speed": speed,
        "wav_path": None,
        "audio_duration": None,
        "leading_silence_ms": None,
        "trailing_silence_ms": None,
        "status": "DRY_RUN" if dry_run else "PENDING",
        "error_detail": None,
    }
    diagnostic_items.append(outro_diag)

    # 4. Print Transcripts & Transformations
    print("TRANSCRIPT COMPARISON & SEMANTIC BOUNDARIES:")
    print("-" * 80)
    print(f"{'Idx':<4} {'Type':<6} {'Boundary':<16} {'Pause(s)':<9} {'Spoken Text Sent to Kokoro'}")
    print("-" * 80)

    for item in diagnostic_items:
        pause_s = f"{(item['pause_duration_ms'] / 1000):.1f}s"
        print(f"{item['index']:<4} {item['segment_type']:<6} {item['boundary_type']:<16} {pause_s:<9} {item['spoken_text']}")
        if item["transformations"]:
            print("     Transformations:")
            for t in item["transformations"]:
                print(f"       * '{t.get('original')}' -> '{t.get('spoken')}' ({t.get('rule')})")

    print("-" * 80)

    # Always write diagnostics artifact
    diag_file = output_dir / "tts-chunks.json"
    summary_file = output_dir / "test-reel-summary.json"

    if dry_run:
        diag_file.write_text(json.dumps(diagnostic_items, indent=2), encoding="utf-8")
        summary = {
            "status": "DRY_RUN",
            "total_chunks": len(diagnostic_items),
            "expected_chunks": len(diagnostic_items),
            "successful_chunks": 0,
            "failed_chunks": [],
            "dry_run": True,
            "voice": voice,
            "speed": speed,
            "herald_canonical": "Herald",
            "herald_spoken": effective_herald_spoken,
            "diagnostics_file": str(diag_file.resolve()),
        }
        summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\n[DRY RUN COMPLETE] Diagnostics written to: {diag_file.name}")
        return 0

    # 5. Live Synthesis
    kokoro = KokoroClient(base_url=effective_url)
    chunk_wav_paths: list[Path] = []
    boundary_types: list[str] = []
    pause_durations: list[float] = []
    failed_chunks: list[dict[str, Any]] = []

    print("\nSynthesizing audio chunks via Kokoro...")

    for item in diagnostic_items:
        idx = item["index"]
        seg_type = item["segment_type"]
        wav_file = chunks_dir / f"reel_{seg_type.lower()}_{idx:04d}.wav"

        print(f"  Synthesizing Chunk {idx}/{len(diagnostic_items) - 1} ({seg_type} - {item['boundary_type']})...")
        try:
            if seg_type in ("INTRO", "OUTRO"):
                res = synthesize_branding_segment(
                    text=item["canonical_text"],
                    spoken_text=item["spoken_text"],
                    output_wav_path=wav_file,
                    kokoro_client=kokoro,
                    voice=voice,
                    speed=speed,
                    segment_name=seg_type.lower(),
                )
                dur = res.get("duration_seconds", 0.0)
            else:
                kokoro.synthesize_chunk(
                    text=item["spoken_text"],
                    output_path=wav_file,
                    voice=voice,
                    speed=speed,
                )
                validate_audio_file(wav_file)
                info = inspect_pcm_wav_file(wav_file)
                dur = float(info["duration_seconds"]) if info else 0.0

            s_info = measure_wav_silence(wav_file)
            lead_s = s_info.get("leading_silence_s")
            trail_s = s_info.get("trailing_silence_s")
            lead_ms = int(round(lead_s * 1000)) if lead_s is not None else None
            trail_ms = int(round(trail_s * 1000)) if trail_s is not None else None

            item["wav_path"] = str(wav_file)
            item["audio_duration"] = dur
            item["leading_silence_ms"] = lead_ms
            item["trailing_silence_ms"] = trail_ms
            item["status"] = "COMPLETED"

            chunk_wav_paths.append(wav_file)
            boundary_types.append(item["boundary_type"])
            pause_durations.append(item["pause_duration_ms"] / 1000.0)

            lead_disp = f"{lead_ms}ms" if lead_ms is not None else "N/A"
            trail_disp = f"{trail_ms}ms" if trail_ms is not None else "N/A"
            print(
                f"    -> Success ({dur:.2f}s, natural silence: lead={lead_disp}, trail={trail_disp} | "
                f"inserted pause: {item['pause_duration_ms']}ms)"
            )

        except Exception as exc:
            err_msg = str(exc)
            item["status"] = "FAILED"
            item["error_detail"] = err_msg
            failed_chunks.append({"index": idx, "segment_type": seg_type, "error": err_msg})
            print(f"    [ERROR] Chunk {idx} ({seg_type}) synthesis failed: {err_msg}")

    # Write diagnostics artifact with synthesis results
    diag_file.write_text(json.dumps(diagnostic_items, indent=2), encoding="utf-8")

    # 6. Failure Handling (Fail Closed)
    if failed_chunks or len(chunk_wav_paths) != len(diagnostic_items):
        print("\n" + "!" * 80)
        print(" [ACCEPTANCE FAILURE] Test reel synthesis is incomplete!")
        print(f" Total Expected: {len(diagnostic_items)} | Successfully Synthesized: {len(chunk_wav_paths)}")
        for fc in failed_chunks:
            print(f"  - Chunk {fc['index']} ({fc['segment_type']}): {fc['error']}")
        print("!" * 80)

        summary = {
            "status": "FAILED",
            "total_chunks": len(diagnostic_items),
            "expected_chunks": len(diagnostic_items),
            "successful_chunks": len(chunk_wav_paths),
            "failed_chunks": failed_chunks,
            "dry_run": False,
            "voice": voice,
            "speed": speed,
            "herald_spoken": effective_herald_spoken,
            "diagnostics_file": str(diag_file.resolve()),
        }
        summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        if allow_partial and chunk_wav_paths:
            partial_mp3 = output_dir / "debug_partial_reel.mp3"
            print(f"\n[DEBUG] Assembling partial audio as requested: {partial_mp3.name}...")
            try:
                join_and_normalize_audio(
                    chunk_paths=chunk_wav_paths,
                    output_mp3_path=partial_mp3,
                    boundary_types=boundary_types,
                    pause_durations=pause_durations,
                    episode_title="Herald Phase 2 Test Reel (PARTIAL DEBUG)",
                    episode_description="Incomplete test reel for debugging",
                    job_id="phase2-reel-partial",
                )
                print(f"[DEBUG] Partial MP3 created at: {partial_mp3}")
            except Exception as pe:
                print(f"[DEBUG] Partial assembly failed: {pe}")

        return 1

    # 7. Final MP3 Assembly
    final_mp3 = output_dir / f"herald_phase2_test_reel_{voice}.mp3"
    print(f"\nAssembling {len(chunk_wav_paths)} chunks into normalized MP3: {final_mp3.name}...")
    try:
        res = join_and_normalize_audio(
            chunk_paths=chunk_wav_paths,
            output_mp3_path=final_mp3,
            boundary_types=boundary_types,
            pause_durations=pause_durations,
            episode_title=f"Herald Phase 2 Test Reel ({voice})",
            episode_description="Phase 2 Narration and Audio Quality Acceptance Benchmark",
            job_id="phase2-reel",
        )
        val = validate_audio_file(final_mp3)

        summary = {
            "status": "SUCCESS",
            "total_chunks": len(diagnostic_items),
            "expected_chunks": len(diagnostic_items),
            "successful_chunks": len(chunk_wav_paths),
            "failed_chunks": [],
            "dry_run": False,
            "voice": voice,
            "speed": speed,
            "herald_canonical": "Herald",
            "herald_spoken": effective_herald_spoken,
            "mp3_path": str(final_mp3.resolve()),
            "duration_seconds": val["duration_seconds"],
            "file_size_bytes": val["size_bytes"],
            "sha256": res.get("sha256"),
            "diagnostics_file": str(diag_file.resolve()),
        }
        summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        print("\n" + "=" * 80)
        print(" TEST REEL ACCEPTANCE ARTIFACT COMPLETE")
        print("=" * 80)
        print(f"MP3 Path:        {final_mp3.resolve()}")
        print(f"Duration:        {val['duration_seconds']:.2f} seconds")
        print(f"File Size:       {val['size_bytes'] / (1024 * 1024):.2f} MB ({val['size_bytes']} bytes)")
        print(f"Diagnostics:     {diag_file.resolve()}")
        print(f"Summary:         {summary_file.resolve()}")
        print(f"SHA256:          {res.get('sha256', 'N/A')}")
        print("=" * 80)
        return 0

    except Exception as e:
        print(f"[ERROR] Audio assembly failed: {e}")
        summary = {
            "status": "ASSEMBLY_FAILED",
            "error": str(e),
            "diagnostics_file": str(diag_file.resolve()),
        }
        summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Herald Phase 2 Test Reel")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("logs/test_reel"),
        help="Directory to save generated test reel audio and transcripts",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate tts-chunks.json diagnostics and print transcripts without calling Kokoro",
    )
    parser.add_argument(
        "--voice",
        type=str,
        default=getattr(settings, "KOKORO_VOICE", "af_heart"),
        help="Kokoro voice to use (e.g. af_heart, bm_george, bf_emma)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=getattr(settings, "KOKORO_SPEED", 1.0),
        help="Kokoro speech speed (default 1.0)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=getattr(settings, "KOKORO_BASE_URL", "http://localhost:8880/v1"),
        help="Kokoro base URL (e.g. http://localhost:8880/v1)",
    )
    parser.add_argument(
        "--lexicon",
        type=Path,
        default=None,
        help="Path to custom pronunciation lexicon JSON or YAML for candidate testing",
    )
    parser.add_argument(
        "--herald-override",
        type=str,
        default=None,
        help="Candidate pronunciation override for 'Herald' (e.g. 'HAIR-uld') for A/B testing",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Debug flag to assemble debug_partial_reel.mp3 on failure instead of exiting immediately",
    )
    args = parser.parse_args()

    sys.exit(
        run_test_reel(
            output_dir=args.output_dir,
            dry_run=args.dry_run,
            voice=args.voice,
            speed=args.speed,
            base_url=args.base_url,
            lexicon_path=args.lexicon,
            herald_override=args.herald_override,
            allow_partial=args.allow_partial,
        )
    )


if __name__ == "__main__":
    main()
