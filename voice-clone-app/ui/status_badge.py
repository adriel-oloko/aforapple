"""Small status pill widget: a colored dot + label.
Port of the `StatusBadge` React component in LiveRealtimeEditor.tsx.
"""

from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel
from PyQt6.QtCore import Qt

from ui.theme import STATUS_COLORS, STATUS_LABELS


class StatusBadge(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._status = "idle"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(6)

        self._dot = QLabel()
        self._dot.setFixedSize(8, 8)
        layout.addWidget(self._dot, alignment=Qt.AlignmentFlag.AlignVCenter)

        self._label = QLabel(STATUS_LABELS["idle"])
        self._label.setStyleSheet("font-size: 11px; font-weight: 500; color: rgba(255,255,255,0.45);")
        layout.addWidget(self._label)

        self.setStyleSheet(
            "StatusBadge { border: 1px solid rgba(255,255,255,0.10); }"
        )
        self._apply()

    def set_status(self, status: str):
        """status: one of idle/requesting/connecting/live/listening/speaking/error"""
        self._status = status if status in STATUS_COLORS else "idle"
        self._apply()

    def _apply(self):
        color = STATUS_COLORS[self._status]
        self._dot.setStyleSheet(f"background-color: {color}; border-radius: 4px;")
        self._label.setText(STATUS_LABELS[self._status])
