"""Generate pre-recorded filler-word/-phrase WAV files for a voice profile
using Fish Audio TTS, cloning the profile's own reference voice.

Each profile under profiles/<Name>/ is expected to already contain:
  - a reference audio clip (e.g. "elon.m4a")
  - a same-named .txt transcript of that clip (e.g. "elon.txt")

Generated filler clips are written into that same profile folder, named
after the slugified filler text (see services.voice_profiles.filler_slug),
which is how services.voice_profiles.load_profile() discovers them
alongside the reference audio at runtime.

Usage:
    python generate_fillers.py --profile elon
    python generate_fillers.py --profile Elon --force
    python generate_fillers.py --list
"""

import argparse
import sys
from pathlib import Path

# Add parent dir so we can import services.config
sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.config import load_config
from services.voice_profiles import (
    FILLER_PHRASES,
    FILLER_TEXTS,
    PROFILES_DIR,
    filler_slug,
    list_profile_names,
    load_profile,
)
from fish_audio_sdk import ReferenceAudio, Session, TTSRequest
import soundfile as sf
import numpy as np

OUTPUT_SAMPLE_RATE = 44100


def resolve_profile_dir(name: str) -> Path:
    """Match ``name`` against the profiles/ subfolders case-insensitively.

    "python generate_fillers.py --profile elon" should work whether the
    folder on disk is named "elon", "Elon", or "ELON" -- profile names are
    displayed/selected case-insensitively everywhere else in the app (see
    services.voice_profiles.list_profile_names), so the CLI shouldn't be
    stricter than the UI.
    """
    available = list_profile_names()
    for candidate in available:
        if candidate.lower() == name.lower():
            return PROFILES_DIR / candidate

    if not available:
        print(f"ERROR: no profiles found under {PROFILES_DIR}/")
    else:
        print(f"ERROR: no profile named '{name}' under {PROFILES_DIR}/")
        print(f"Available profiles: {', '.join(available)}")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Generate filler WAVs for a voice profile")
    parser.add_argument(
        "--profile",
        help="Profile folder name under profiles/ (case-insensitive), e.g. 'elon'",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available profile names and exit",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate filler clips even if they already exist",
    )
    args = parser.parse_args()

    if args.list:
        available = list_profile_names()
        if not available:
            print(f"No profiles found under {PROFILES_DIR}/")
        else:
            print("Available profiles:")
            for name in available:
                print(f"  {name}")
        return

    if not args.profile:
        parser.error("--profile is required (or pass --list to see available profiles)")

    config = load_config()
    if not config.fish_key:
        print("ERROR: FISH_API_KEY not set in .env")
        sys.exit(1)

    profile_dir = resolve_profile_dir(args.profile)
    profile = load_profile(profile_dir.name)
    if profile is None or profile.reference_audio is None:
        print(
            f"ERROR: {profile_dir}/ has no reference audio clip "
            "(expected a .wav/.m4a file that isn't a recognized filler, "
            "plus a same-named .txt transcript)."
        )
        sys.exit(1)
    if not profile.reference_transcript:
        print(
            f"ERROR: {profile_dir}/ is missing the transcript for "
            f"{profile.reference_audio.name} "
            f"(expected {profile.reference_audio.with_suffix('.txt').name})."
        )
        sys.exit(1)

    print(
        f"Using profile '{profile.name}': "
        f"{profile.reference_audio.name} + {profile.reference_audio.with_suffix('.txt').name}"
    )

    reference = ReferenceAudio(
        audio=profile.reference_audio.read_bytes(),
        text=profile.reference_transcript,
    )
    session = Session(config.fish_key)

    for text in FILLER_TEXTS + FILLER_PHRASES:
        out_path = profile_dir / f"{filler_slug(text)}.wav"
        if out_path.exists() and not args.force:
            print(f"Skip (exists): {out_path.name}")
            continue

        print(f"Generating: '{text}' -> {out_path.name} ...")
        request = TTSRequest(
            text=text,
            format="pcm",
            sample_rate=OUTPUT_SAMPLE_RATE,
            references=[reference],
            latency="balanced",
            chunk_length=100,
        )

        chunks = []
        for chunk in session.tts(request, backend="s2.1-pro"):
            if chunk:
                chunks.append(chunk)

        if not chunks:
            print(f"  WARNING: no audio returned for '{text}'")
            continue

        raw = b"".join(chunks)
        usable_len = len(raw) - (len(raw) % 2)
        pcm = np.frombuffer(raw[:usable_len], dtype=np.int16)

        # Trim leading/trailing silence
        threshold = 200
        abs_pcm = np.abs(pcm)
        non_silent = np.where(abs_pcm > threshold)[0]
        if len(non_silent) > 0:
            pcm = pcm[non_silent[0]:non_silent[-1] + 1]

        sf.write(str(out_path), pcm, OUTPUT_SAMPLE_RATE)
        print(f"  Done: {len(pcm)} samples ({len(pcm)/OUTPUT_SAMPLE_RATE:.1f}s)")

    print(f"\nFiller WAVs saved to {profile_dir}/")


if __name__ == "__main__":
    main()
