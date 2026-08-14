"""Session logging.

Mirrors what a dev console gives you: a timestamped, human-readable
stream of what the app is doing (session start/stop, payment events,
STT/TTS pipeline events, errors), except we *also* persist it to a file
per run so it can be reviewed after the window closes.

Usage:
    from services.session_log import get_logger
    log = get_logger()
    log.info("Session started")
    log.error("Token request failed: %s", err)

Every run creates a fresh file at APP_DIR/logs/session-YYYYMMDD-HHMMSS.log
(directory created on first use). The console handler prints the same
lines you'd see in the log file, so running `python main.py` from a
terminal gives you the live console feed, and the file gives you a
persistent copy. In the frozen exe, logs land next to the exe.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from services.app_paths import APP_DIR

_LOG_DIR = APP_DIR / "logs"
_logger: logging.Logger | None = None
_log_file_path: Path | None = None


def get_logger() -> logging.Logger:
    global _logger, _log_file_path
    if _logger is not None:
        return _logger

    _LOG_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    _log_file_path = _LOG_DIR / f"session-{timestamp}.log"

    logger = logging.getLogger("voice_clone_studio")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(_log_file_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    logger.info("Session log started -> %s", _log_file_path)
    _logger = logger
    return logger


def log_file_path() -> Path | None:
    """Returns the path of the current run's log file, once get_logger()
    has been called at least once."""
    return _log_file_path
