"""Entrypoint. Uses qasync to run an asyncio event loop alongside Qt's,
since the WebRTC signaling (services/fal_client.py) and peer connection
(aiortc, in ui/live_editor_widget.py) are async-native and need a real
event loop, not just Qt's.
"""

import sys

import qasync
from PyQt6.QtWidgets import QApplication

from services.config import load_config
from ui.main_window import MainWindow
from ui.theme import DARK_QSS


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)

    loop = qasync.QEventLoop(app)
    import asyncio

    asyncio.set_event_loop(loop)

    config = load_config()
    window = MainWindow(config, loop)
    window.show()

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
