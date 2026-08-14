"""Loads API keys from a local .env file.

This is a desktop app, so unlike the original Next.js project there's no
browser to hide these keys from -- they're just read directly into the
Python process. Keep your .env out of version control regardless.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env_file(path: Path) -> None:
    """Load the .env file, tolerating files that aren't valid UTF-8.

    python-dotenv's load_dotenv() always opens the file as utf-8. On
    Windows it's easy to end up with a .env saved as cp1252/"ANSI"
    (e.g. Notepad, or an editor that auto-"smart-quotes" a dash or
    ellipsis in a comment line) -- that raises
    UnicodeDecodeError: 'utf-8' codec can't decode byte 0x85 ...
    the moment load_dotenv() reads the file.

    To be robust to that, if the file exists but isn't valid UTF-8, we
    re-encode it to UTF-8 in memory (via stream_from_str) instead of
    letting the app crash on startup over a text-encoding mismatch.
    """
    if not path.exists():
        return

    try:
        raw = path.read_bytes()
        raw.decode("utf-8")
    except UnicodeDecodeError:
        from dotenv import load_dotenv as _load_dotenv  # noqa: F401
        from dotenv.main import DotEnv

        text = raw.decode("cp1252", errors="replace")
        DotEnv(
            dotenv_path=None,
            stream=__import__("io").StringIO(text),
            verbose=True,
            interpolate=True,
            override=False,
            encoding="utf-8",
        ).set_as_environment_variables()
        return

    load_dotenv(path)


_load_env_file(_ENV_PATH)


@dataclass
class Config:
    fal_key: str | None
    assemblyai_key: str | None
    fish_key: str | None

    def missing_for_video(self) -> list[str]:
        missing = []
        if not self.fal_key:
            missing.append("FAL_KEY")
        return missing

    def missing_for_voice_clone(self) -> list[str]:
        missing = []
        if not self.assemblyai_key:
            missing.append("ASSEMBLYAI_API_KEY")
        if not self.fish_key:
            missing.append("FISH_API_KEY")
        return missing


def load_config() -> Config:
    return Config(
        fal_key=os.getenv("FAL_KEY") or None,
        assemblyai_key=os.getenv("ASSEMBLYAI_API_KEY") or None,
        fish_key=os.getenv("FISH_API_KEY") or None,
    )
