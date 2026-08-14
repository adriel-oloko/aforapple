"""BEP20/ERC20 stablecoin payment detection for the app's receiving address(es).

Billing model
-------------
The app has ONE USDT (BEP20) receiving address on BNB Smart Chain. Users
top up by sending USDT on BSC to that address. This module polls the
chain for Transfer events *to* the address, converts each payment to
minutes of credit at the configured rate (default $7.00/minute), and
hands the payments to whoever called poll().

There is no smart contract and no forwarding step: the money lands
directly in the receiving wallet, and the app simply watches it. Credit
is stored locally (see services/metering.py), so the balance is per
machine/install -- that matches the "one address per app" model.

Hidden test credit source
-------------------------
The same tracker is also used for an env-gated *test* credit source
(default: Circle USDC on Ethereum Sepolia) so testers can fund the app
with faucet tokens instead of real money. It is invisible in the UI:
the operator enables it via VOICE_APP_TEST_CREDIT_ENABLED in .env and
the second tracker simply credits the same balance. See config.py.

No API key needed: confirmed blocks are read over public RPC
(eth_getLogs on the token contract).

State
-----
Each tracker persists its own checkpoint (data/payment_state*.json) with
the last scanned block and the set of already-seen (tx_hash, log_index)
pairs so a restart never double-counts and never re-scans the chain.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx

from services.app_paths import DATA_DIR
from services.session_log import get_logger

log = get_logger()
# BEP20 USDT on BNB Smart Chain (Binance-peg, 18 decimals).
USDT_BEP20 = "0x55d398326f99059fF775485246999027B3197955"
# Transfer(address indexed from, address indexed to, uint256 value)
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# How many blocks to walk back on the very first run. Payments older than
# this are not backfilled (the app can't know about payments made before
# it ever ran); 1000 blocks is ~50 minutes of chain history, plenty of
# slack for a payment sent right before the first launch.
_FIRST_RUN_BACKFILL_BLOCKS = 1000

# eth_getLogs is bounded server-side; keep each request well under the limit.
_MAX_BLOCKS_PER_REQUEST = 2000


@dataclass(frozen=True)
class Payment:
    tx_hash: str
    log_index: int
    block_number: int
    amount_usdt: float
    minutes_credited: float
    detected_at: str  # ISO timestamp (UTC)
    source: str = "bsc"  # log/telemetry label, never shown in the UI

    @property
    def key(self) -> str:
        return f"{self.tx_hash}:{self.log_index}"


def _padded_address(address: str) -> str:
    """Topic value for the 'to' address: 32-byte left-padded hex."""
    raw = address.lower().removeprefix("0x")
    return "0x" + raw.zfill(64)


class _RpcRotator:
    """JSON-RPC client that rotates across an endpoint list on failure."""

    def __init__(self, urls: list[str], timeout: float = 15.0):
        self._urls = list(urls)
        self._index = 0
        self._timeout = timeout

    def call(self, method: str, params: list) -> dict:
        last_error: Optional[Exception] = None
        for _ in range(len(self._urls)):
            url = self._urls[self._index % len(self._urls)]
            self._index += 1
            try:
                resp = httpx.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": params,
                    },
                    timeout=self._timeout,
                )
                resp.raise_for_status()
                body = resp.json()
                if "error" in body:
                    raise RuntimeError(f"RPC error: {body['error']}")
                return body
            except Exception as err:  # noqa: BLE001
                last_error = err
                log.warning("RPC %s failed (%s): %s", url, method, err)
        raise RuntimeError(f"All RPC endpoints failed for {method}") from last_error


class PaymentTracker:
    """Scans the chain for USDT transfers to the watch address.

    Not thread-safe by itself: the watcher thread is the only caller of
    poll()/save_state(); the UI thread only reads via the signal the
    watcher emits.
    """

    def __init__(
        self,
        watch_address: str,
        price_usd_per_minute: float,
        rpc_urls: list[str],
        state_path: Optional[Path] = None,
        token_contract: str = USDT_BEP20,
        token_decimals: int = 18,
        source: str = "bsc",
    ):
        self._watch_address = watch_address
        self._price_usd_per_minute = price_usd_per_minute
        self._rpc_urls = list(rpc_urls)
        self._state_path = state_path or (DATA_DIR / "payment_state.json")
        self._token_contract = token_contract
        self._token_decimals = token_decimals
        self._source = source

        self._last_scanned_block: Optional[int] = None
        self._seen: set[str] = set()
        self._rpc_rotator = _RpcRotator(rpc_urls)
        self._load_state()

    # -- state persistence -------------------------------------------------

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        self._last_scanned_block = data.get("last_scanned_block")
        self._seen = set(data.get("seen", []))

    def save_state(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {
                    "last_scanned_block": self._last_scanned_block,
                    "seen": sorted(self._seen),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._state_path)

    # -- RPC ---------------------------------------------------------------

    def _rpc(self, method: str, params: list) -> dict:
        """POST a JSON-RPC call, rotating across the endpoint list."""
        return self._rpc_rotator.call(method, params)

    def latest_block(self) -> int:
        body = self._rpc("eth_blockNumber", [])
        return int(body["result"], 16)

    def _get_transfers(self, from_block: int, to_block: int) -> list[dict]:
        body = self._rpc(
            "eth_getLogs",
            [
                {
                    "address": self._token_contract,
                    "fromBlock": hex(from_block),
                    "toBlock": hex(to_block),
                    "topics": [
                        _TRANSFER_TOPIC,
                        None,
                        _padded_address(self._watch_address),
                    ],
                }
            ],
        )
        return body.get("result") or []

    # -- scanning ----------------------------------------------------------

    def _decode_payment(self, entry: dict) -> Optional[Payment]:
        tx_hash = entry.get("transactionHash") or ""
        log_index = int(entry.get("logIndex") or "0x0", 16)
        block_number = int(entry.get("blockNumber") or "0x0", 16)
        data = entry.get("data") or ""
        # data is "0x" + 32-byte value + 32-byte padding; skip the 0x.
        if len(data) < 66:
            return None
        raw_value = data[2:66]
        amount_wei = int(raw_value, 16)
        amount_usdt = amount_wei / (10 ** self._token_decimals)
        if amount_usdt <= 0:
            return None
        minutes = amount_usdt / self._price_usd_per_minute
        return Payment(
            tx_hash=tx_hash,
            log_index=log_index,
            block_number=block_number,
            amount_usdt=amount_usdt,
            minutes_credited=minutes,
            detected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source=self._source,
        )

    def poll(self) -> list[Payment]:
        """Check for new payments since the last scan. Returns newly
        detected, previously unseen payments.

        On any RPC error the scan aborts without advancing the checkpoint,
        so the next poll retries from the same block -- no gaps.
        """
        latest = self.latest_block()
        start = self._last_scanned_block + 1 if self._last_scanned_block is not None else None
        if start is None:
            start = max(0, latest - _FIRST_RUN_BACKFILL_BLOCKS)
        if start > latest:
            return []

        new_payments: list[Payment] = []
        cursor = start
        while cursor <= latest:
            to_block = min(cursor + _MAX_BLOCKS_PER_REQUEST - 1, latest)
            entries = self._get_transfers(cursor, to_block)
            for entry in entries:
                payment = self._decode_payment(entry)
                if payment is None:
                    continue
                if payment.key in self._seen:
                    continue
                self._seen.add(payment.key)
                new_payments.append(payment)
            cursor = to_block + 1

        self._last_scanned_block = latest
        self.save_state()

        if new_payments:
            for p in new_payments:
                log.info(
                    "Payment detected [%s]: tx=%s block=%d amount=%.2f -> %.2f min",
                    p.source,
                    p.tx_hash,
                    p.block_number,
                    p.amount_usdt,
                    p.minutes_credited,
                )
        return new_payments


class PaymentWatcher(threading.Thread):
    """Background thread that polls the PaymentTracker and hands new
    payments to a callback (the UI emits a Qt signal from it).

    poll_interval_seconds: how often to hit the chain. BSC produces a
    block every ~3s, so a 12s poll finds a payment within ~15s.
    """

    def __init__(
        self,
        tracker: PaymentTracker,
        on_payments: Callable[[list[Payment]], None],
        poll_interval_seconds: int = 12,
    ):
        super().__init__(daemon=True)
        self._tracker = tracker
        self._on_payments = on_payments
        self._poll_interval = poll_interval_seconds
        self._stop_event = threading.Event()
        self._first_poll_delay = 1.0  # let the UI settle before first check

    def run(self) -> None:
        time.sleep(self._first_poll_delay)
        while not self._stop_event.is_set():
            try:
                payments = self._tracker.poll()
                if payments:
                    try:
                        self._on_payments(payments)
                    except Exception:  # noqa: BLE001
                        log.exception("on_payments callback failed")
            except Exception as err:  # noqa: BLE001
                log.warning("Payment poll failed (will retry): %s", err)
            self._stop_event.wait(self._poll_interval)

    def stop(self) -> None:
        self._stop_event.set()


class NativeBalanceWatcher(threading.Thread):
    """Hidden-test-source watcher for NATIVE testnet currency (e.g. Sepolia ETH).

    ERC20-only tracking misses deposits of the chain's native token (no
    Transfer events exist for it). This watcher polls eth_getBalance and
    credits any increase above a persisted floor, so a tester who sends
    plain testnet ETH (the most common faucet output) still gets credit.

    First run: the floor starts at 0, so whatever is already sitting on
    the address is credited once. After that, only real increases credit.
    A balance decrease resets the floor to the lower value (testers never
    withdraw; documented trade-off, test-only, no real assets involved).

    Conversion: minutes = eth_delta / eth_per_minute (configurable).
    """

    def __init__(
        self,
        address: str,
        rpc_urls: list[str],
        price_usd_per_minute: float,
        eth_per_minute: float,
        state_path: Path,
        on_payments: Callable[[list[Payment]], None],
        poll_interval_seconds: int = 12,
    ):
        super().__init__(daemon=True)
        self._address = address
        self._price_usd_per_minute = price_usd_per_minute
        self._eth_per_minute = eth_per_minute
        self._state_path = state_path
        self._on_payments = on_payments
        self._poll_interval = poll_interval_seconds
        self._rpc_rotator = _RpcRotator(rpc_urls)
        self._floor_wei = 0
        self._stop_event = threading.Event()
        self._first_poll_delay = 1.0
        self._load_state()

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._floor_wei = int(data.get("floor_wei", 0))
        except (OSError, json.JSONDecodeError, ValueError):
            self._floor_wei = 0

    def save_state(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"floor_wei": self._floor_wei}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._state_path)

    def _fetch_balance_wei(self) -> int:
        body = self._rpc_rotator.call("eth_getBalance", [self._address, "latest"])
        return int(body["result"], 16)

    def _latest_block(self) -> int:
        body = self._rpc_rotator.call("eth_blockNumber", [])
        return int(body["result"], 16)

    def poll_once(self) -> list[Payment]:
        """One balance check. Returns credits for any increase above the
        floor (empty list if nothing new)."""
        balance = self._fetch_balance_wei()
        if balance == self._floor_wei:
            return []
        if balance < self._floor_wei:
            # Someone withdrew; reset the floor so future deposits count.
            self._floor_wei = balance
            self.save_state()
            return []
        delta_wei = balance - self._floor_wei
        self._floor_wei = balance
        self.save_state()

        minutes = (delta_wei / 1e18) / self._eth_per_minute
        payment = Payment(
            tx_hash=f"native:{balance}",
            log_index=0,
            block_number=self._latest_block(),
            amount_usdt=minutes * self._price_usd_per_minute,
            minutes_credited=minutes,
            detected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source="test-native",
        )
        log.info(
            "Native balance credit [test-native]: +%.6f ETH -> %.2f min (floor now %d)",
            delta_wei / 1e18,
            minutes,
            self._floor_wei,
        )
        return [payment]

    def run(self) -> None:
        time.sleep(self._first_poll_delay)
        while not self._stop_event.is_set():
            try:
                payments = self.poll_once()
                if payments:
                    try:
                        self._on_payments(payments)
                    except Exception:  # noqa: BLE001
                        log.exception("on_payments callback failed")
            except Exception as err:  # noqa: BLE001
                log.warning("Native balance poll failed (will retry): %s", err)
            self._stop_event.wait(self._poll_interval)

    def stop(self) -> None:
        self._stop_event.set()
