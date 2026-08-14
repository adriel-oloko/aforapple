"""Frozen-exe aware path resolution.

This app is distributed as a single .exe (PyInstaller onefile). In a
frozen build, ``__file__`` points inside the temporary extraction dir
(sys._MEIPASS), which is wiped on exit -- so anything that must survive
the process (profiles, .env, logs, balance data) has to live next to the
exe, not next to the source file.

Two roots are defined here and used by the other services:

  APP_DIR
      The folder the user sees as "the app". Next to the .exe when
      frozen, otherwise the repo root. Writable, persistent.

  BUNDLE_DIR
      Where bundled read-only resources (profiles/, .env.example) are
      extracted to at runtime. sys._MEIPASS when frozen, otherwise the
      repo root (where the plain files already live).

Usage:
    from services.app_paths import APP_DIR, BUNDLE_DIR
    env_path = APP_DIR / ".env"
"""

from __future__ import annotations

import sys
from pathlib import Path


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_dir() -> Path:
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _bundle_dir() -> Path:
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


APP_DIR = _app_dir()
BUNDLE_DIR = _bundle_dir()

# Runtime state lives under APP_DIR/data (created on demand).
DATA_DIR = APP_DIR / "data"
