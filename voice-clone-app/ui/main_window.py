"""Main window for the standalone Voice Clone Studio app.

Layout:
  ┌──────────────────────────────────────────────┐
  │ BALANCE  12.4 min · $86.80   [Top up]        │   <- PaymentWidget
  ├──────────────────────────────────────────────┤
  │ Advanced Audio (Voice Clone) panel           │   <- AdvancedAudioWidget
  │  profile / devices / start-stop / transcript │
  └──────────────────────────────────────────────┘

The window owns the two timers/threads that make the app a *paid* app:

  - A 1-second QTimer that debits the prepaid meter while the voice
    clone session is running and stops the session when it hits zero.
  - The payment watcher thread that polls BNB Smart Chain for USDT
    transfers to the app's receiving address and credits the meter.

Nothing in this file talks to the audio/AI services directly; it only
wires the meter + payment signals into the widget that does.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QMainWindow, QScrollArea, QVBoxLayout, QWidget

from services.config import Config
from services.metering import Meter
from services.payment import PaymentTracker, PaymentWatcher
from services.session_log import get_logger
from ui.advanced_audio_widget import AdvancedAudioWidget
from ui.payment_widget import PaymentWidget

log = get_logger()


class _PaymentBridge(QObject):
    """Qt signal bridge so the plain payment watcher thread can hand
    results to the GUI thread safely."""

    payments_detected = pyqtSignal(list)


class MainWindow(QMainWindow):
    def __init__(self, config: Config, meter: Meter):
        super().__init__()
        self._config = config
        self._meter = meter
        self.setWindowTitle("Voice Clone Studio")
        self.resize(980, 860)

        self._build_ui()

        self._bridge = _PaymentBridge()
        self._bridge.payments_detected.connect(self._on_payments_detected)

        self._tracker = PaymentTracker(
            watch_address=config.usdt_address,
            price_usd_per_minute=config.price_usd_per_minute,
            rpc_urls=config.rpc_urls,
        )
        self._watchers: list = [
            PaymentWatcher(
                tracker=self._tracker,
                on_payments=lambda pays: self._bridge.payments_detected.emit(pays),
                poll_interval_seconds=config.poll_seconds,
            )
        ]
        log.info(
            "Payment watcher started: address=%s price=$%.2f/min poll=%ds",
            config.usdt_address,
            config.price_usd_per_minute,
            config.poll_seconds,
        )

        # Hidden test credit source (env-gated, invisible in the UI). Same
        # tracker machinery on a testnet, own state file so the production
        # checkpoint is never touched. Credits flow into the same meter.
        if config.test_credit_enabled:
            from services.app_paths import DATA_DIR

            test_tracker = PaymentTracker(
                watch_address=config.test_watch_address,
                price_usd_per_minute=config.test_price_usd_per_minute,
                rpc_urls=config.test_rpc_urls,
                state_path=DATA_DIR / "payment_state_test.json",
                token_contract=config.test_token_contract,
                token_decimals=config.test_token_decimals,
                source="test",
            )
            self._watchers.append(
                PaymentWatcher(
                    tracker=test_tracker,
                    on_payments=lambda pays: self._bridge.payments_detected.emit(pays),
                    poll_interval_seconds=config.test_poll_seconds,
                )
            )
            log.info(
                "Test credit source enabled: token=%s address=%s price=$%.2f/min",
                config.test_token_contract,
                config.test_watch_address,
                config.test_price_usd_per_minute,
            )

            # Native testnet currency (e.g. Sepolia ETH) also credits, so a
            # tester who sends plain ETH gets topped up too. First poll
            # credits whatever is already on the address.
            if config.test_native_eth_per_minute > 0:
                from services.payment import NativeBalanceWatcher

                self._watchers.append(
                    NativeBalanceWatcher(
                        address=config.test_watch_address,
                        rpc_urls=config.test_rpc_urls,
                        price_usd_per_minute=config.test_price_usd_per_minute,
                        eth_per_minute=config.test_native_eth_per_minute,
                        state_path=DATA_DIR / "test_native_state.json",
                        on_payments=lambda pays: self._bridge.payments_detected.emit(pays),
                        poll_interval_seconds=config.test_poll_seconds,
                    )
                )
                log.info(
                    "Native testnet credit enabled: %.4f ETH/min",
                    config.test_native_eth_per_minute,
                )

        for watcher in self._watchers:
            watcher.start()

        # 1s meter tick: bill only while the session is active.
        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(1000)
        self._meter_timer.timeout.connect(self._on_meter_tick)
        self._meter_timer.start()

    # -- UI ---------------------------------------------------------------

    def _build_ui(self):
        self._payment_widget = PaymentWidget(self._config, self._meter)
        self._audio_widget = AdvancedAudioWidget(self._config, meter=self._meter)
        self._audio_widget.set_expanded(True)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(self._payment_widget)
        layout.addWidget(self._audio_widget)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(container)
        self.setCentralWidget(scroll)

    # -- meter tick -------------------------------------------------------

    def _on_meter_tick(self):
        if not self._audio_widget.is_running():
            return
        new_balance = self._meter.debit_tick()
        self._payment_widget.refresh_balance()
        if new_balance <= 0:
            log.warning("Balance exhausted; stopping voice clone session")
            self._audio_widget.stop_for_insufficient_balance()

    # -- payment events ---------------------------------------------------

    def _on_payments_detected(self, payments: list):
        for p in payments:
            self._meter.credit_minutes(p.minutes_credited)
            self._payment_widget.notify_payment(p.amount_usdt, p.minutes_credited)
            log.info(
                "Credited %.2f min from %.2f USDT (tx %s)",
                p.minutes_credited,
                p.amount_usdt,
                p.tx_hash,
            )

    # -- lifecycle --------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802
        log.info("Main window closing")
        self._meter_timer.stop()
        for watcher in getattr(self, "_watchers", []):
            watcher.stop()
        self._audio_widget.shutdown()
        self._meter.save()
        super().closeEvent(event)
