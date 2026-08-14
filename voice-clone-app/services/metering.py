"""Prepaid balance meter for pay-per-minute voice cloning.

Balance is stored in *minutes* of voice-clone time. Credits arrive from
the payment watcher (USDT sent to the app's receiving address, converted
at $7.00/minute by default); usage debits 1/60 of a minute per second
while the voice-clone session is active.

The balance is local to this machine/install and persists across runs in
data/balance.json. It is a plain prepaid wallet: money lands in the
receiving USDT address on-chain, and this file is the app's record of
how much of that has been converted into usable minutes.

Thread-safety: a lock guards all mutations. The Qt timer debits on the
GUI thread and the payment watcher credits on its worker thread (via a
Qt signal that lands on the GUI thread anyway); the lock keeps both
safe regardless.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from services.app_paths import DATA_DIR
from services.session_log import get_logger

log = get_logger()

# Seconds of active usage per meter tick. 1 second of usage == 1/60 min.
_TICK_SECONDS = 1.0
_SECONDS_PER_MINUTE = 60.0


class Meter:
    def __init__(
        self,
        price_usd_per_minute: float,
        balance_path: Optional[Path] = None,
        on_balance_changed: Optional[Callable[[float], None]] = None,
    ):
        self._price = price_usd_per_minute
        self._balance_minutes = 0.0
        self._total_credited_minutes = 0.0
        self._path = balance_path or (DATA_DIR / "balance.json")
        self._lock = threading.Lock()
        self._on_balance_changed = on_balance_changed
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._balance_minutes = float(data.get("balance_minutes", 0.0))
            self._total_credited_minutes = float(data.get("total_credited_minutes", 0.0))
        except (OSError, json.JSONDecodeError, ValueError):
            self._balance_minutes = 0.0
            self._total_credited_minutes = 0.0

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "balance_minutes": round(self._balance_minutes, 6),
                    "total_credited_minutes": round(self._total_credited_minutes, 6),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    # -- state -------------------------------------------------------------

    @property
    def balance_minutes(self) -> float:
        with self._lock:
            return self._balance_minutes

    @property
    def balance_usd(self) -> float:
        return self.balance_minutes * self._price

    @property
    def price_usd_per_minute(self) -> float:
        return self._price

    def _notify(self) -> None:
        if self._on_balance_changed is not None:
            try:
                self._on_balance_changed(self._balance_minutes)
            except Exception:  # noqa: BLE001
                pass

    # -- mutations ---------------------------------------------------------

    def credit_minutes(self, minutes: float) -> float:
        """Add credit (from a detected payment). Returns the new balance."""
        if minutes <= 0:
            return self.balance_minutes
        with self._lock:
            self._balance_minutes += minutes
            self._total_credited_minutes += minutes
            new_balance = self._balance_minutes
        log.info(
            "Balance credited +%.2f min (%.2f USDT) -> %.2f min total",
            minutes,
            minutes * self._price,
            new_balance,
        )
        self.save()
        self._notify()
        return new_balance

    def debit_tick(self) -> float:
        """Deduct one meter tick (1 second == 1/60 minute). Returns the
        new balance, which may be negative by a fraction of a tick; the
        caller stops the session when it drops to <= 0."""
        with self._lock:
            self._balance_minutes -= _TICK_SECONDS / _SECONDS_PER_MINUTE
            new_balance = self._balance_minutes
        self._notify()
        return new_balance

    def can_afford(self) -> bool:
        return self.balance_minutes > 0.0
