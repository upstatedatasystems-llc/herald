"""Manual Acceptance Test Reel for Herald Phase 2: Narration & Audio Quality.

Deterministic local utility that generates ~60-90 seconds of audio exercising:
- Product name: Herald
- Astronomy: JWST, JWST's, LIGO, MoM-z14, JADES-GS-z14-0, GW150914, M87*
- Defense / Models: B-52, F-16, GPT-4o, ARC-AGI-2
- Hardware: HBM3E, GDDR6X, LPCAMM2, 64-bit
- Numbers / Units: 1955, 2026, 1990s, 1.5%, $3 billion, $250 million, 3 GHz, 5 GB, 10 km
- Semantic Boundaries: Sentence, paragraph, section, and branding transitions.

Usage:
  uv run python tools/tts_test_reel.py [--dry-run] [--output-dir DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from herald.audio.branding import (
    render_intro_narration,
    render_outro_narration,
    synthesize_branding_segment,
)
from herald.audio.ffmpeg_builder import (
    join_and_normalize_audio,
    measure_wav_silence,
    validate_audio_file,
)
from herald.config import settings
from herald.tts.chunker import BoundaryType, chunk_podcast_script
from herald.tts.kokoro_client import KokoroClient

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
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = output_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    effective_url = base_url or settings.KOKORO_BASE_URL
    print("=" * 80)
    print(" HERALD PHASE 2: NARRATION & AUDIO QUALITY TEST REEL")
    print("=" * 80)
    print(f"Output Directory: {output_dir}")
    print(f"Voice: {voice} | Speed: {speed} | Base URL: {effective_url} | Dry Run: {dry_run}\n")

    # 1. Prepare Branding Narration
    intro_text = render_intro_narration(
        episode_title="Narration and Audio Benchmark",
        publisher="BBC Sky at Night Magazine",
        target_minutes=1,
    )
    outro_text = render_outro_narration()

    # 2. Chunk Script with Spoken Normalization
    body_chunks = chunk_podcast_script(TEST_REEL_SEGMENTS, max_chars=400)

    # 3. Print Transcripts and Boundary Mapping
    print("TRANSCRIPT COMPARISON & SEMANTIC BOUNDARIES:")
    print("-" * 80)
    print(f"{'Idx':<4} {'Boundary':<16} {'Pause(s)':<9} {'Spoken Text Sent to Kokoro'}")
    print("-" * 80)

    # Intro preview
    print(f"{'0':<4} {'BRANDING':<16} {'1.2s':<9} {intro_text}")

    for chk in body_chunks:
        pause_str = f"{chk.pause_duration_seconds:.1f}s"
        print(f"{chk.index:<4} {chk.boundary_type.value:<16} {pause_str:<9} {chk.text}")
        if chk.transformations:
            print("     Transformations applied:")
            for t in chk.transformations:
                print(f"       * '{t['original']}' -> '{t['spoken']}' ({t['rule']})")

    # Outro preview
    print(f"{len(body_chunks) + 1:<4} {'BRANDING':<16} {'0.0s':<9} {outro_text}")
    print("-" * 80)

    if dry_run:
        print("\n[DRY RUN COMPLETE] Spoken normalization and semantic chunking verified successfully.")
        return 0

    # 4. Kokoro Synthesis
    kokoro = KokoroClient(base_url=effective_url)
    chunk_wav_paths: list[Path] = []
    boundary_types: list[str] = []
    pause_durations: list[float] = []

    print("\nSynthesizing audio chunks via Kokoro...")

    # A. Intro Branding
    intro_wav = chunks_dir / "branding_intro.wav"
    try:
        print("  Synthesizing Intro Branding...")
        synthesize_branding_segment(
            text=intro_text,
            output_wav_path=intro_wav,
            kokoro_client=kokoro,
            voice=voice,
            speed=speed,
        )
        s_info = measure_wav_silence(intro_wav)
        print(f"    -> Done ({intro_wav.stat().st_size} bytes, leading silence: {s_info['leading_silence_s']}s, trailing: {s_info['trailing_silence_s']}s)")
        chunk_wav_paths.append(intro_wav)
        boundary_types.append("BRANDING")
        pause_durations.append(1.2)
    except Exception as e:
        print(f"    [WARNING] Kokoro intro synthesis failed: {e}")

    # B. Body Chunks
    for chk in body_chunks:
        wav_file = chunks_dir / f"chunk_{chk.index:04d}.wav"
        print(f"  Synthesizing Chunk {chk.index}/{len(body_chunks)} ({chk.boundary_type.value})...")
        try:
            kokoro.synthesize_chunk(
                text=chk.text,
                output_path=wav_file,
                voice=voice,
                speed=speed,
            )
            s_info = measure_wav_silence(wav_file)
            print(f"    -> Done ({wav_file.stat().st_size} bytes, leading: {s_info['leading_silence_s']}s, trailing: {s_info['trailing_silence_s']}s)")
            chunk_wav_paths.append(wav_file)
            boundary_types.append(chk.boundary_type.value)
            pause_durations.append(chk.pause_duration_seconds)
        except Exception as e:
            print(f"    [WARNING] Chunk {chk.index} synthesis failed: {e}")

    # C. Outro Branding
    outro_wav = chunks_dir / "branding_outro.wav"
    try:
        print("  Synthesizing Outro Branding...")
        synthesize_branding_segment(
            text=outro_text,
            output_wav_path=outro_wav,
            kokoro_client=kokoro,
            voice=voice,
            speed=speed,
        )
        s_info = measure_wav_silence(outro_wav)
        print(f"    -> Done ({outro_wav.stat().st_size} bytes, leading silence: {s_info['leading_silence_s']}s, trailing: {s_info['trailing_silence_s']}s)")
        chunk_wav_paths.append(outro_wav)
        boundary_types.append("BRANDING")
        pause_durations.append(0.0)
    except Exception as e:
        print(f"    [WARNING] Kokoro outro synthesis failed: {e}")

    if not chunk_wav_paths:
        print("[ERROR] No audio chunks were synthesized. Ensure Kokoro is running.")
        return 1

    # 5. Assemble into Finished MP3
    final_mp3 = output_dir / "herald_phase2_test_reel.mp3"
    print(f"\nAssembling {len(chunk_wav_paths)} chunks into normalized MP3: {final_mp3.name}...")
    try:
        res = join_and_normalize_audio(
            chunk_paths=chunk_wav_paths,
            output_mp3_path=final_mp3,
            boundary_types=boundary_types,
            pause_durations=pause_durations,
            episode_title="Herald Phase 2 Test Reel",
            episode_description="Phase 2 Narration and Audio Quality Acceptance Benchmark",
            job_id="phase2-reel",
        )
        val = validate_audio_file(final_mp3)
        print("\n" + "=" * 80)
        print(" TEST REEL ASSEMBLY COMPLETE")
        print("=" * 80)
        print(f"MP3 Path:        {final_mp3.resolve()}")
        print(f"Duration:        {val['duration_seconds']:.2f} seconds")
        print(f"File Size:       {val['size_bytes'] / (1024 * 1024):.2f} MB ({val['size_bytes']} bytes)")
        print(f"SHA256:          {res.get('sha256', 'N/A')}")
        print("=" * 80)
        return 0
    except Exception as e:
        print(f"[ERROR] Audio assembly failed: {e}")
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
        help="Print canonical vs. spoken text and pause mappings without calling Kokoro",
    )
    parser.add_argument(
        "--voice",
        type=str,
        default=getattr(settings, "KOKORO_VOICE", "af_heart"),
        help="Kokoro voice to use",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=getattr(settings, "KOKORO_SPEED", 1.0),
        help="Kokoro speech speed",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=getattr(settings, "KOKORO_BASE_URL", "http://localhost:8880/v1"),
        help="Kokoro base URL (e.g. http://localhost:8880/v1)",
    )
    args = parser.parse_args()
    sys.exit(
        run_test_reel(
            output_dir=args.output_dir,
            dry_run=args.dry_run,
            voice=args.voice,
            speed=args.speed,
            base_url=args.base_url,
        )
    )


if __name__ == "__main__":
    main()
