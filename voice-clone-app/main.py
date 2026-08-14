"""Voice Clone Studio entrypoint.

Standalone PyQt6 desktop app: mic -> AssemblyAI (STT) -> Fish Audio
(cloned-voice TTS) -> speakers, billed per minute at $7.00 (paid in
advance via BEP20 USDT to the app's single receiving address).

Unlike the video Live Editor this was extracted from, the voice pipeline
is pure threading (no WebRTC, no asyncio), so this is a plain QApplication
with a 1-second meter QTimer -- no qasync needed.

Usage:
    python main.py                 # run the app
    python main.py --self-check    # build everything offscreen, verify
                                   # imports/paths, print status, exit 0.
                                   # Used to validate the bundled exe.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from services.app_paths import APP_DIR, BUNDLE_DIR
from services.config import load_config
from services.metering import Meter
from services.payment import PaymentTracker
from services.session_log import get_logger, log_file_path
from ui.main_window import MainWindow
from ui.theme import DARK_QSS


def _window_icon() -> QIcon:
    candidates = [
        BUNDLE_DIR / "ui" / "app-icon.ico",
        Path(__file__).resolve().parent / "ui" / "app-icon.ico",
    ]
    for path in candidates:
        if path.exists():
            return QIcon(str(path))
    return QIcon()


def _self_check() -> int:
    """Offscreen construction test for the frozen exe: proves the bundle
    can import everything, resolve paths, build the full window, and talk
    to the BSC RPC (best effort)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    log = get_logger()
    log.info("Self-check starting (frozen=%s)", bool(getattr(sys, "frozen", False)))

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(DARK_QSS)

    config = load_config()
    log.info("APP_DIR=%s", APP_DIR)
    log.info("BUNDLE_DIR=%s", BUNDLE_DIR)
    log.info("Profiles dir=%s exists=%s", APP_DIR / "profiles", (APP_DIR / "profiles").exists())
    log.info("Watch address=%s price=$%.2f/min", config.usdt_address, config.price_usd_per_minute)

    meter = Meter(config.price_usd_per_minute)
    window = MainWindow(config, meter)
    window.show()
    app.processEvents()

    # Best-effort RPC reachability check (network may be off; not fatal).
    tracker = PaymentTracker(
        watch_address=config.usdt_address,
        price_usd_per_minute=config.price_usd_per_minute,
        rpc_urls=config.rpc_urls,
    )
    try:
        block = tracker.latest_block()
        log.info("BSC RPC OK: latest block %d", block)
    except Exception as err:  # noqa: BLE001
        log.warning("BSC RPC unreachable in self-check: %s", err)

    window.close()
    app.processEvents()
    # In a --windowed exe there is no console; persist the result next to
    # the exe so the operator can read it, and also try stdout.
    result = "SELF-CHECK OK"
    try:
        print(result)
    except Exception:  # noqa: BLE001
        pass
    try:
        (APP_DIR / "selfcheck.log").write_text(result, encoding="utf-8")
    except OSError:
        pass
    log.info("Self-check passed")
    return 0


def main() -> int:
    if "--self-check" in sys.argv[1:]:
        return _self_check()

    log = get_logger()
    log.info("Starting Voice Clone Studio")

    app = QApplication(sys.argv)
    app.setApplicationName("Voice Clone Studio")
    app.setStyleSheet(DARK_QSS)
    app.setWindowIcon(_window_icon())

    config = load_config()
    missing = config.missing_for_voice_clone()
    if missing:
        log.warning(
            "Missing config for voice clone: %s (edit .env next to the app)",
            ", ".join(missing),
        )

    meter = Meter(config.price_usd_per_minute)
    window = MainWindow(config, meter)
    window.show()

    log.info("Window shown. Logging to %s", log_file_path())
    exit_code = app.exec()
    log.info("App exiting (code=%s)", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
