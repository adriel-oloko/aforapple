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
load_dotenv(_ENV_PATH)


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
