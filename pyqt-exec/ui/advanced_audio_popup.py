"""Standalone popup window for the Advanced Audio controls.

Shown in place of the (now hidden/obscured) inline Advanced Audio
section whenever the Live Editor's output view is expanded to fill the
app viewport -- expanding the video takes over the space the inline
panel lives in, so the Stop button, live transcript blocks, and filler
controls move into this separate top-level window instead of
disappearing.

This is a normal (not always-on-top) window: it gets its own taskbar
entry, can be moved to another monitor, alt-tabbed to, etc. It does
NOT own the pipeline itself -- AdvancedAudioWidget still owns the
AssemblyAIStreamer/FishVoiceCloneSpeaker/MicCapture instances. This
widget just borrows AdvancedAudioWidget's TextBlockList and Stop
button by re-parenting them in and returning them on close, so there's
exactly one source of truth for pipeline state either way.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ui.status_badge import StatusBadge


class AdvancedAudioPopup(QWidget):
    """Hosts the live pipeline controls in their own window.

    close_requested is emitted when the window is closed (X button or
    the "Return to main window" button) so MainWindow can re-embed the
    borrowed widgets back into the inline AdvancedAudioWidget panel.
    """

    close_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Voice Clone -- Advanced Audio")
        self.resize(560, 640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        header_row = QHBoxLayout()
        title = QLabel("Advanced · Audio (Voice Clone)")
        title.setObjectName("sectionTitle")
        header_row.addWidget(title)
        header_row.addStretch(1)
        layout.addLayout(header_row)

        controls_row = QHBoxLayout()
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("secondary")
        controls_row.addWidget(self.stop_btn)
        controls_row.addStretch(1)
        self.stt_badge = StatusBadge()
        controls_row.addWidget(self.stt_badge)
        self.tts_badge = StatusBadge()
        controls_row.addWidget(self.tts_badge)
        layout.addLayout(controls_row)

        # A placeholder slot the TextBlockList gets reparented into --
        # see MainWindow._show_advanced_popup / _hide_advanced_popup.
        self.body_layout = QVBoxLayout()
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.body_layout)

        layout.addStretch(1)

        hint = QLabel("Collapse the video view to return these controls to the main window.")
        hint.setObjectName("placeholder")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    def closeEvent(self, event):  # noqa: N802
        self.close_requested.emit()
        super().closeEvent(event)
