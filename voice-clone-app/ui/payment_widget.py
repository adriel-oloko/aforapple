"""Payment / balance UI: a balance bar at the top of the window plus a
top-up dialog that shows the app's single BEP20 USDT receiving address
with a QR code and copy button.

The dialog does not move money itself: the user sends USDT (BEP20) on
BNB Smart Chain to the address, the payment watcher detects the transfer
on-chain, and the meter is credited. The dialog just displays live
status while that happens.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from services.config import Config
from services.metering import Meter
from services.session_log import get_logger

log = get_logger()

try:  # QR rendering is optional; the dialog works without it.
    import qrcode  # noqa: F401
    from PIL.ImageQt import ImageQt

    _QR_AVAILABLE = True
except Exception:  # noqa: BLE001
    ImageQt = None
    _QR_AVAILABLE = False


def _format_minutes(minutes: float) -> str:
    if minutes >= 1.0:
        return f"{minutes:.2f} min"
    seconds = int(round(minutes * 60))
    return f"{seconds} sec"


class TopUpDialog(QDialog):
    def __init__(self, config: Config, meter: Meter, parent=None):
        super().__init__(parent)
        self._config = config
        self._meter = meter
        self.setWindowTitle("Top up balance")
        self.setMinimumWidth(460)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("TOP UP BALANCE")
        title.setObjectName("fieldLabel")
        layout.addWidget(title)

        price = QLabel(
            f"Voice cloning costs ${self._meter.price_usd_per_minute:.2f} per minute. "
            f"Send USDT (BEP20) on BNB Smart Chain to the address below — your "
            f"balance tops up automatically when the payment is detected."
        )
        price.setWordWrap(True)
        price.setStyleSheet("color: rgba(255,255,255,0.7);")
        layout.addWidget(price)

        # QR code + address side by side.
        row = QHBoxLayout()
        row.setSpacing(16)

        qr_box = QFrame()
        qr_box.setObjectName("sectionFrame")
        qr_layout = QVBoxLayout(qr_box)
        qr_layout.setContentsMargins(10, 10, 10, 10)
        qr_label = QLabel()
        qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr_pix = self._render_qr()
        if qr_pix is not None:
            qr_label.setPixmap(qr_pix)
        else:
            qr_label.setText("QR unavailable")
            qr_label.setObjectName("placeholder")
        qr_layout.addWidget(qr_label)
        row.addWidget(qr_box)

        addr_col = QVBoxLayout()
        addr_col.setSpacing(8)
        addr_hint = QLabel("USDT (BEP20) — BNB Smart Chain")
        addr_hint.setObjectName("placeholder")
        addr_col.addWidget(addr_hint)

        self._address_edit = QLineEdit(self._config.usdt_address)
        self._address_edit.setReadOnly(True)
        addr_col.addWidget(self._address_edit)

        copy_btn = QPushButton("Copy address")
        copy_btn.setObjectName("secondary")
        copy_btn.clicked.connect(self._copy_address)
        addr_col.addWidget(copy_btn)

        rate_note = QLabel(
            f"Rate: {self._meter.price_usd_per_minute:.2f} USDT = 1 minute. "
            "Payments are detected within ~15-30 seconds."
        )
        rate_note.setWordWrap(True)
        rate_note.setObjectName("placeholder")
        addr_col.addWidget(rate_note)
        addr_col.addStretch(1)

        row.addLayout(addr_col, 1)
        layout.addLayout(row)

        # Live payment status line, updated by the main window.
        self._status_label = QLabel(
            "Watching for payments… your balance will update automatically."
        )
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: rgba(255,255,255,0.55);")
        layout.addWidget(self._status_label)

        self._balance_label = QLabel("")
        self._balance_label.setStyleSheet(
            "color: rgba(255,255,255,0.9); font-weight: 600;"
        )
        layout.addWidget(self._balance_label)
        self._refresh_balance()

        close_btn = QPushButton("Close")
        close_btn.setObjectName("secondary")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

    def _render_qr(self):
        if not _QR_AVAILABLE:
            return None
        try:
            import qrcode
            from PIL.ImageQt import ImageQt
            from PyQt6.QtGui import QPixmap

            img = qrcode.make(self._config.usdt_address).get_image()
            qimage = ImageQt(img)
            pix = QPixmap.fromImage(qimage)
            return pix.scaled(
                200, 200, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        except Exception as err:  # noqa: BLE001
            log.warning("QR render failed: %s", err)
            return None

    def _copy_address(self):
        QGuiApplication.clipboard().setText(self._config.usdt_address)
        self._status_label.setText("Address copied to clipboard.")

    def _refresh_balance(self):
        self._balance_label.setText(
            f"Current balance: {_format_minutes(self._meter.balance_minutes)} "
            f"(${self._meter.balance_usd:.2f})"
        )

    # Public API used by the main window --------------------------------

    def notify_payment(self, amount_usdt: float, minutes: float):
        self._status_label.setText(
            f"✓ Payment detected: +{amount_usdt:.2f} USDT → "
            f"+{minutes:.2f} min credited."
        )
        self._status_label.setStyleSheet("color: rgba(120,255,120,0.9);")
        self._refresh_balance()

    def notify_status(self, message: str):
        self._status_label.setText(message)


class PaymentWidget(QFrame):
    """Balance bar at the top of the main window."""

    def __init__(self, config: Config, meter: Meter, parent=None):
        super().__init__(parent)
        self.setObjectName("sectionFrame")
        self._config = config
        self._meter = meter
        self._build_ui()

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(14)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("BALANCE")
        title.setObjectName("fieldLabel")
        title_col.addWidget(title)

        self._balance_label = QLabel("")
        self._balance_label.setStyleSheet(
            "font-size: 15px; font-weight: 600; color: rgba(255,255,255,0.95);"
        )
        title_col.addWidget(self._balance_label)
        layout.addLayout(title_col, 1)

        self._watch_label = QLabel("")
        self._watch_label.setObjectName("placeholder")
        self._watch_label.setWordWrap(True)
        layout.addWidget(self._watch_label, 1)

        topup_btn = QPushButton("Top up")
        topup_btn.setObjectName("primary")
        topup_btn.clicked.connect(self._open_topup)
        layout.addWidget(topup_btn)

        self._refresh_balance()

    def _open_topup(self):
        dialog = TopUpDialog(self._config, self._meter, self)
        self._topup_dialog = dialog
        dialog.exec()

    def _refresh_balance(self):
        minutes = self._meter.balance_minutes
        self._balance_label.setText(
            f"{_format_minutes(minutes)}  ·  ${self._meter.balance_usd:.2f}"
        )

    # Public API used by the main window --------------------------------

    def refresh_balance(self):
        self._refresh_balance()
        dialog = getattr(self, "_topup_dialog", None)
        if dialog is not None and dialog.isVisible():
            dialog._refresh_balance()

    def set_watch_status(self, message: str):
        self._watch_label.setText(message)

    def notify_payment(self, amount_usdt: float, minutes: float):
        self.set_watch_status(
            f"Payment received: +{amount_usdt:.2f} USDT → +{minutes:.2f} min"
        )
        dialog = getattr(self, "_topup_dialog", None)
        if dialog is not None and dialog.isVisible():
            dialog.notify_payment(amount_usdt, minutes)
        self.refresh_balance()
