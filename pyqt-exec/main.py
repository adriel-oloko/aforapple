"""Entrypoint. Uses qasync to run an asyncio event loop alongside Qt's,
since the WebRTC signaling (services/fal_client.py) and peer connection
(aiortc, in ui/live_editor_widget.py) are async-native and need a real
event loop, not just Qt's.
"""

import sys

import qasync
from PyQt6.QtWidgets import QApplication

from services import win_ifaddr_patch
from services.config import load_config
from services.session_log import get_logger, log_file_path
from ui.main_window import MainWindow
from ui.theme import DARK_QSS


def main():
    # Must run before any aiortc/aioice import path can create an
    # RTCPeerConnection and trigger ICE-gathering's call into ifaddr,
    # which crashes on Windows if any network adapter's internal name
    # isn't valid UTF-8 (see services/win_ifaddr_patch.py). No-op on
    # non-Windows platforms.
    win_ifaddr_patch.apply()

    log = get_logger()
    log.info("Starting Live Editor app")

    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)

    loop = qasync.QEventLoop(app)
    import asyncio

    asyncio.set_event_loop(loop)

    config = load_config()
    missing_video = config.missing_for_video()
    missing_voice = config.missing_for_voice_clone()
    if missing_video:
        log.warning("Missing config for Live Editor: %s", ", ".join(missing_video))
    if missing_voice:
        log.warning("Missing config for Advanced Audio: %s", ", ".join(missing_voice))

    window = MainWindow(config, loop)
    window.show()

    log.info("Window shown. Logging to %s", log_file_path())

    try:
        with loop:
            loop.run_forever()
    finally:
        log.info("App exiting")


if __name__ == "__main__":
    main()
