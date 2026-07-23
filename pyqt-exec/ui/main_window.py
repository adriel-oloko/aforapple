"""Top-level window: Live Editor (Lucy 2.5 video) stacked above the
collapsible Advanced Audio (Voice Clone) section, inside a scroll area
so the window works on smaller screens.
"""

from __future__ import annotations

import asyncio

from PyQt6.QtWidgets import QMainWindow, QScrollArea, QVBoxLayout, QWidget

from services.config import Config
from ui.advanced_audio_widget import AdvancedAudioWidget
from ui.live_editor_widget import LiveEditorWidget


class MainWindow(QMainWindow):
    def __init__(self, config: Config, async_loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.setWindowTitle("Live Editor")
        self.resize(1100, 900)

        self._live_editor = LiveEditorWidget(config, async_loop)
        self._advanced_audio = AdvancedAudioWidget(config)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 24)
        layout.setSpacing(0)
        layout.addWidget(self._live_editor)

        audio_wrapper = QWidget()
        audio_layout = QVBoxLayout(audio_wrapper)
        audio_layout.setContentsMargins(24, 0, 24, 0)
        audio_layout.addWidget(self._advanced_audio)
        layout.addWidget(audio_wrapper)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        self.setCentralWidget(scroll)

    def closeEvent(self, event):  # noqa: N802
        self._live_editor.shutdown()
        self._advanced_audio.shutdown()
        super().closeEvent(event)
