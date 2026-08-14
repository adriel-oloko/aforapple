"""Standalone popup window for the Live Editor's video output feed.

Opened whenever "Expand view" is clicked -- rather than taking over
LiveEditorWidget's own rect in place, the edited (Lucy 2.5) output
feed gets its own top-level window here, the same way the Advanced
Audio controls get their own AdvancedAudioPopup window (see
ui/advanced_audio_popup.py). "Expand view" only ever opens *this*
window; it does not open, close, or otherwise touch the Advanced
Audio popup.

This is a normal (not always-on-top) window: it gets its own taskbar
entry, can be moved to another monitor, alt-tabbed to, etc. It does
NOT own the WebRTC session itself -- LiveEditorWidget still owns the
RTCPeerConnection/webcam capture and is the single source of truth for
session state. This widget only displays whatever frame/status
LiveEditorWidget hands it and reports back when it should close.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ui.status_badge import StatusBadge


class _ScalingImageLabel(QLabel):
    """Local copy of LiveEditorWidget's `_ScalingVideoLabel` helper --
    duplicated here (rather than imported) so this module doesn't need
    to import live_editor_widget.py, which is the module that creates
    and owns this popup (importing it back would be circular)."""

    def __init__(self, placeholder_text: str):
        super().__init__(placeholder_text)
        self._source_pixmap: Optional[QPixmap] = None

    def set_source_pixmap(self, pixmap: QPixmap) -> None:
        self._source_pixmap = pixmap
        self._rescale()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._source_pixmap is None or self._source_pixmap.isNull():
            return
        fitted = self._source_pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        super().setPixmap(fitted)


class VideoOutputPopup(QWidget):
    """Hosts the Lucy 2.5 output feed, full-size, in its own window.

    close_requested is emitted when the window is closed -- via the X
    button or the Escape key -- so LiveEditorWidget can flip "Expand
    view" back to its collapsed label/state.
    """

    close_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Live Editor -- Output (Lucy 2.5)")
        self.resize(960, 640)
        self.setStyleSheet("background-color: #000;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(16, 12, 16, 8)
        title = QLabel("Output")
        title.setObjectName("sectionTitle")
        header_row.addWidget(title)
        header_row.addStretch(1)
        self.status_badge = StatusBadge()
        header_row.addWidget(self.status_badge)
        layout.addLayout(header_row)

        self._image_label = _ScalingImageLabel("")
        self._image_label.setObjectName("placeholder")
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        layout.addWidget(self._image_label, stretch=1)

        hint = QLabel("Press Esc to collapse")
        hint.setObjectName("placeholder")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(
            "color: rgba(255,255,255,0.25); font-size: 11px; padding: 6px;"
        )
        layout.addWidget(hint)

        # Escape needs to work regardless of which child widget (if
        # any) has focus inside this window -- same reasoning as the
        # window-level shortcut LiveEditorWidget used to own for the
        # in-place overlay it had before this popup replaced it.
        self._escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._escape_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._escape_shortcut.activated.connect(self.close)

    def set_source_pixmap(self, pixmap: QPixmap) -> None:
        self._image_label.set_source_pixmap(pixmap)

    def set_status(self, status: str) -> None:
        self.status_badge.set_status(status)

    def closeEvent(self, event):  # noqa: N802
        self.close_requested.emit()
        super().closeEvent(event)
