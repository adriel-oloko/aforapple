"""Voice-clone profiles.

Each subfolder of profiles/ is a self-contained "voice profile" that
bundles everything the Advanced Audio panel needs for one voice:

  - a reference audio clip of the voice to clone (e.g. "og.wav")
  - a same-named .txt transcript of that clip (Fish Audio's on-the-fly
    clone mode requires the exact transcript)
  - filler clips, one per entry in FILLER_TEXTS or FILLER_PHRASES, named
    after the slugified filler text (e.g. "so_ummm.wav", "i_mean.wav")
  - profile.json (auto-generated): stores the Fish Audio persistent
    model ID so the reference audio doesn't have to be re-embedded on
    every TTS request.

This module only *discovers and resolves* those pieces from disk; the
UI (AdvancedAudioWidget / TextBlockList) is responsible for putting
them where they belong once a profile is selected.
"""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"

# Kept here (rather than duplicated in ui/text_block_list.py) since the
# slug derived from each entry is also what identifies a filler clip on
# disk -- see _filler_slug() below.
FILLER_TEXTS = [
    "Ummm",
    "So ummm",
    "You know",
    "Like",
    "I mean",
    "Basically",
]

# Longer, phrase-length fillers -- rendered as their own row in the UI,
# below FILLER_TEXTS, but resolved from disk the same way (slugified
# filename match).
FILLER_PHRASES = [
    "So um you know like basically,",
    "So yeah, I mean, kind of, um,",
    "Okay so basically, like, the thing is,",
    "Um, so I guess, you know, sort of,",
    "Right, so honestly, like, um, basically,",
    "So anyway, I mean, you know, like,",
    "So um, like, honestly, it's kind of a whole thing right now.",
    "I mean, yeah, no, it's fine, it's just — you know, a lot.",
    "Okay so basically, um, long story short, it worked out.",
    "Well, I guess, sort of, we're figuring it out as we go.",
    "So yeah, um, to be honest, I haven't really thought about it.",
]

_AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac")


def filler_slug(text: str) -> str:
    """Filename stem for a filler clip. Strips punctuation (phrases carry
    trailing commas, e.g. "So um you know like basically,") and joins
    words with underscores, e.g. "so_um_you_know_like_basically"."""
    words = "".join(ch if ch.isalnum() or ch.isspace() else "" for ch in text.lower())
    return "_".join(words.split())


# Kept as a private alias since older code/imports may still reference the
# original underscored name.
_filler_slug = filler_slug

# text -> slug, precomputed once, across both filler lists.
_SLUG_TO_TEXT = {filler_slug(t): t for t in FILLER_TEXTS + FILLER_PHRASES}


@dataclass
class VoiceProfile:
    name: str
    dir: Path
    reference_audio: Optional[Path] = None
    reference_transcript: str = ""
    reference_id: Optional[str] = None  # Fish Audio persistent model _id
    filler_paths: dict[str, Path] = field(default_factory=dict)  # filler text -> wav path

    def filler_path(self, text: str) -> Optional[Path]:
        return self.filler_paths.get(text)

    @property
    def has_persistent_model(self) -> bool:
        """True if a Fish Audio model already exists for this voice."""
        return bool(self.reference_id)


def list_profile_names() -> list[str]:
    """Names of the subfolders under profiles/, sorted case-insensitively.

    Returns an empty list (rather than raising) if profiles/ doesn't
    exist yet -- the UI just shows an empty dropdown in that case.
    """
    if not PROFILES_DIR.exists():
        return []
    return sorted(
        (p.name for p in PROFILES_DIR.iterdir() if p.is_dir()),
        key=str.lower,
    )


def load_profile(name: str) -> Optional[VoiceProfile]:
    """Load a profile by its folder name under profiles/.

    Walks the folder once, classifying each audio file as either a
    filler clip (its filename slug matches one of FILLER_TEXTS or
    FILLER_PHRASES) or, for the first audio file that isn't a
    recognized filler, the voice-to-clone reference clip. The
    reference clip's transcript is read from the .txt file with the
    same stem, if present.

    Returns None if the folder doesn't exist.
    """
    profile_dir = PROFILES_DIR / name
    if not profile_dir.is_dir():
        return None

    reference_audio: Optional[Path] = None
    filler_paths: dict[str, Path] = {}

    for path in sorted(profile_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _AUDIO_EXTS:
            continue
        filler_text = _SLUG_TO_TEXT.get(path.stem.lower())
        if filler_text is not None:
            filler_paths[filler_text] = path
        elif reference_audio is None:
            reference_audio = path

    reference_transcript = ""
    if reference_audio is not None:
        txt_path = reference_audio.with_suffix(".txt")
        if txt_path.exists():
            reference_transcript = txt_path.read_text(
                encoding="utf-8", errors="replace"
            ).strip()

    reference_id = _load_profile_json(profile_dir).get("reference_id")

    return VoiceProfile(
        name=name,
        dir=profile_dir,
        reference_audio=reference_audio,
        reference_transcript=reference_transcript,
        reference_id=reference_id,
        filler_paths=filler_paths,
    )


def _profile_json_path(profile_dir: Path) -> Path:
    return profile_dir / "profile.json"


def _load_profile_json(profile_dir: Path) -> dict:
    path = _profile_json_path(profile_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_profile_json(profile_dir: Path, data: dict) -> None:
    path = _profile_json_path(profile_dir)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def create_fish_model(
    api_key: str,
    profile: VoiceProfile,
    *,
    title: Optional[str] = None,
    visibility: str = "private",
) -> str:
    """Upload the profile's reference audio to Fish Audio as a persistent model.

    Returns the model ``_id`` (use as ``reference_id`` in TTS requests).

    Saves the returned id into the profile's ``profile.json`` so subsequent
    sessions can reuse it without re-uploading.
    """
    if profile.reference_audio is None:
        raise ValueError("Profile has no reference audio to upload")

    audio_bytes = profile.reference_audio.read_bytes()
    filename = profile.reference_audio.name
    mime_type = (
        mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    title = title or f"{profile.name} (voice changer)"

    form_data = {
        "type": "tts",
        "title": title,
        "visibility": visibility,
        "train_mode": "fast",
    }
    if profile.reference_transcript:
        form_data["texts"] = profile.reference_transcript

    files = {"voices": (filename, audio_bytes, mime_type)}

    resp = httpx.post(
        "https://api.fish.audio/model",
        headers={"Authorization": f"Bearer {api_key}"},
        data=form_data,
        files=files,
        timeout=30,
    )
    resp.raise_for_status()
    model = resp.json()
    model_id: str = model["_id"]

    # Save it so we don't have to re-upload next time.
    existing = _load_profile_json(profile.dir)
    existing["reference_id"] = model_id
    _save_profile_json(profile.dir, existing)

    return model_id
