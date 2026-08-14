"""Dev verification for Voice Clone Studio (not shipped in the exe).

Run with the project venv's Python (Windows side):
    ..\..\.venv\Scripts\python.exe verify.py

Covers: module imports, config defaults, meter math + persistence,
payment log decoding (synthetic), live read-only BSC RPC check for
recent USDT transfers to the watch address, and offscreen construction
of the full main window.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    # 1. Imports
    from services import audio_devices, config as config_mod, metering, payment, voice_profiles
    from services.app_paths import APP_DIR as APP_DIR_RESOLVED
    from services.assemblyai_stream import AssemblyAIStreamer
    from services.fish_audio_client import FishVoiceCloneSpeaker
    from services.mic_capture import MicCapture
    from services.session_log import get_logger

    get_logger()
    check("all service modules import", True)

    # 2. Config defaults
    cfg = config_mod.load_config()
    check(
        "usdt address default",
        cfg.usdt_address.lower() == "0x217153d3cc0ca2e72d633af400b554d5bb1ab5f8",
        cfg.usdt_address,
    )
    check("price default 7", cfg.price_usd_per_minute == 7.0, str(cfg.price_usd_per_minute))
    check("poll seconds >= 5", cfg.poll_seconds >= 5, str(cfg.poll_seconds))
    check("rpc urls non-empty", len(cfg.rpc_urls) >= 1, str(len(cfg.rpc_urls)))
    check("app dir resolves", APP_DIR_RESOLVED == APP_DIR, str(APP_DIR_RESOLVED))

    # 2b. Hidden test credit source config
    check("test source enabled", cfg.test_credit_enabled is True, str(cfg.test_credit_enabled))
    check(
        "test token = sepolia usdc",
        cfg.test_token_contract.lower() == "0x1c7d4b196cb0c7b01d743fbc6116a902379c7238",
        cfg.test_token_contract,
    )
    check("test decimals = 6", cfg.test_token_decimals == 6, str(cfg.test_token_decimals))
    check("test rpc urls non-empty", len(cfg.test_rpc_urls) >= 1, str(len(cfg.test_rpc_urls)))
    check(
        "test native eth/min default",
        abs(cfg.test_native_eth_per_minute - 0.01) < 1e-9,
        str(cfg.test_native_eth_per_minute),
    )

    # 3. Profiles discoverable
    names = voice_profiles.list_profile_names()
    check("profiles listed", "Elon" in names, str(names))
    prof = voice_profiles.load_profile("Elon")
    check(
        "Elon profile has reference + model",
        prof is not None and prof.reference_audio is not None and prof.has_persistent_model,
        str(prof.reference_id if prof else None),
    )

    # 4. Meter math + persistence (temp file so we don't touch real data/)
    tmpdir = Path(tempfile.mkdtemp(prefix="vcs-verify-"))
    balance_path = tmpdir / "balance.json"
    m = metering.Meter(price_usd_per_minute=7.0, balance_path=balance_path)
    m.credit_minutes(14.0)
    check("credit 14 min", abs(m.balance_minutes - 14.0) < 1e-9, str(m.balance_minutes))
    check("balance usd = 98", abs(m.balance_usd - 98.0) < 1e-6, str(m.balance_usd))
    for _ in range(60):
        m.debit_tick()
    check("debit 60 ticks = 1 min", abs(m.balance_minutes - 13.0) < 1e-6, str(m.balance_minutes))
    m.save()
    m2 = metering.Meter(price_usd_per_minute=7.0, balance_path=balance_path)
    check("persist round-trip", abs(m2.balance_minutes - 13.0) < 1e-6, str(m2.balance_minutes))
    m2.credit_minutes(1.0)
    check("can_afford", m2.can_afford())

    # 5. Payment log decoding (synthetic)
    from services import payment as payment_mod

    tracker = payment_mod.PaymentTracker(
        watch_address=cfg.usdt_address,
        price_usd_per_minute=7.0,
        rpc_urls=cfg.rpc_urls,
        state_path=tmpdir / "payment_state.json",
    )
    # 7 USDT = 1 min; 14 USDT = 2 min
    one_usdt_wei = 10**18
    value_hex = one_usdt_wei.to_bytes(32, "big").hex()
    entry_7 = {
        "transactionHash": "0x" + "ab" * 32,
        "logIndex": "0x0",
        "blockNumber": "0x10",
        "data": "0x" + value_hex + "00" * 32,  # 1e18 wei = 1 USDT
    }
    p1 = tracker._decode_payment(entry_7)
    check("decode 1 USDT", p1 is not None and abs(p1.amount_usdt - 1.0) < 1e-9, str(p1))
    check("1 USDT = 1/7 min", p1 is not None and abs(p1.minutes_credited - 1 / 7) < 1e-9, str(p1.minutes_credited if p1 else None))

    entry_bad = {
        "transactionHash": "0x" + "cd" * 32,
        "logIndex": "0x1",
        "blockNumber": "0x11",
        "data": "0x1234",  # too short -> reject
    }
    check("reject malformed log", tracker._decode_payment(entry_bad) is None)

    # 6. Live read-only BSC RPC: latest block + recent transfers to the address
    try:
        latest = tracker.latest_block()
        check("BSC RPC latest block", latest > 0, f"block {latest}")
        recent = tracker._get_transfers(max(0, latest - 2000), latest)
        print(f"  transfers to address in last 2000 blocks: {len(recent)}")
        for entry in recent[:5]:
            p = tracker._decode_payment(entry)
            if p:
                print(f"    {p.amount_usdt:.2f} USDT tx={p.tx_hash[:18]}... block={p.block_number}")

        # Positive control: USDT Transfer logs happen every block on BSC.
        # Query topic0=Transfer only (no address filter) on the USDT
        # contract for ONE recent block, trying each RPC endpoint, and
        # print the raw response so a throttled/empty reply is visible.
        # If the same RPC path returns logs, the pipeline genuinely sees
        # chain data (i.e. a real payment to the watch address would be
        # detected the same way).
        import httpx as _httpx

        control_logs: list = []
        for url in cfg.rpc_urls:
            try:
                control_body = _httpx.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_getLogs",
                        "params": [
                            {
                                "address": payment_mod.USDT_BEP20,
                                "fromBlock": hex(latest - 1),
                                "toBlock": hex(latest - 1),
                                "topics": [payment_mod._TRANSFER_TOPIC],
                            }
                        ],
                    },
                    timeout=15,
                ).json()
            except Exception as err:  # noqa: BLE001
                print(f"    control {url}: exception {err}")
                continue
            if "error" in control_body:
                print(f"    control {url}: error {control_body['error']}")
                continue
            control_logs = control_body.get("result") or []
            if control_logs:
                print(f"    control {url}: {len(control_logs)} logs in block {latest - 1}")
                break
        check(
            "wildcard USDT logs visible",
            len(control_logs) > 0,
            f"{len(control_logs)} logs",
        )
        for entry in control_logs[:3]:
            p = tracker._decode_payment(entry)
            if p:
                print(f"    control: {p.amount_usdt:.2f} USDT tx={p.tx_hash[:18]}... block={p.block_number}")
    except Exception as err:  # noqa: BLE001
        check("BSC RPC live check", False, str(err))

    # 6b. Live read-only Sepolia check for the hidden test source.
    test_tracker = payment_mod.PaymentTracker(
        watch_address=cfg.test_watch_address,
        price_usd_per_minute=cfg.test_price_usd_per_minute,
        rpc_urls=cfg.test_rpc_urls,
        state_path=tmpdir / "payment_state_test.json",
        token_contract=cfg.test_token_contract,
        token_decimals=cfg.test_token_decimals,
        source="test",
    )
    # Synthetic 6-decimal decode: 1 USDC (1e6 raw) -> 1/7 min.
    one_usdc_raw = 10**6
    entry_usdc = {
        "transactionHash": "0x" + "12" * 32,
        "logIndex": "0x0",
        "blockNumber": "0x10",
        "data": "0x" + one_usdc_raw.to_bytes(32, "big").hex() + "00" * 32,
    }
    p_usdc = test_tracker._decode_payment(entry_usdc)
    check(
        "test decode 1 USDC (6 dec)",
        p_usdc is not None
        and abs(p_usdc.amount_usdt - 1.0) < 1e-9
        and p_usdc.source == "test",
        str(p_usdc.amount_usdt if p_usdc else None),
    )
    try:
        sepolia_latest = test_tracker.latest_block()
        check("Sepolia RPC latest block", sepolia_latest > 0, f"block {sepolia_latest}")

        # Verify the token's on-chain decimals() really is 6 (no guessing).
        import httpx as _httpx

        dec_body = _httpx.post(
            cfg.test_rpc_urls[0],
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [
                    {"to": cfg.test_token_contract, "data": "0x313ce567"},
                    "latest",
                ],
            },
            timeout=15,
        ).json()
        dec_hex = (dec_body.get("result") or "0x")
        check("Sepolia USDC decimals() == 6", int(dec_hex, 16) == 6, dec_hex)

        # Wildcard control on the Sepolia USDC contract.
        test_logs: list = []
        for url in cfg.test_rpc_urls:
            try:
                body = _httpx.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_getLogs",
                        "params": [
                            {
                                "address": cfg.test_token_contract,
                                "fromBlock": hex(sepolia_latest - 20),
                                "toBlock": hex(sepolia_latest),
                                "topics": [payment_mod._TRANSFER_TOPIC],
                            }
                        ],
                    },
                    timeout=15,
                ).json()
            except Exception as err:  # noqa: BLE001
                print(f"    sepolia control {url}: exception {err}")
                continue
            if "error" in body:
                print(f"    sepolia control {url}: error {body['error']}")
                continue
            test_logs = body.get("result") or []
            if test_logs:
                print(f"    sepolia control {url}: {len(test_logs)} logs in last 20 blocks")
                break
        check(
            "Sepolia USDC logs visible",
            len(test_logs) > 0,
            f"{len(test_logs)} logs",
        )
    except Exception as err:  # noqa: BLE001
        check("Sepolia RPC live check", False, str(err))

    # 6c. Native balance watcher: delta math, floor persistence, live read.
    from services.payment import NativeBalanceWatcher

    native_state = tmpdir / "test_native_state.json"
    collected: list = []
    native = NativeBalanceWatcher(
        address=cfg.test_watch_address,
        rpc_urls=cfg.test_rpc_urls,
        price_usd_per_minute=7.0,
        eth_per_minute=0.01,
        state_path=native_state,
        on_payments=collected.extend,
    )
    check("native floor starts at 0", native._floor_wei == 0, str(native._floor_wei))

    # First poll: 0.0018 ETH on the address -> credited at 0.01 ETH/min.
    native._fetch_balance_wei = lambda: int(0.0018 * 1e18)  # type: ignore[method-assign]
    pays = native.poll_once()
    check("native first-poll credits existing balance", len(pays) == 1, str(len(pays)))
    check(
        "native credit math",
        bool(pays) and abs(pays[0].minutes_credited - 0.18) < 1e-9
        and pays[0].source == "test-native",
        str(pays[0].minutes_credited if pays else None),
    )
    check("native floor persisted", native._floor_wei == int(0.0018 * 1e18))
    check("native state file written", native_state.exists())

    # Same balance again -> no double credit.
    check("native no credit on flat balance", native.poll_once() == [])

    # +0.0002 ETH deposit -> credited as delta only.
    native._fetch_balance_wei = lambda: int(0.0020 * 1e18)  # type: ignore[method-assign]
    pays2 = native.poll_once()
    check(
        "native delta credit",
        bool(pays2) and abs(pays2[0].minutes_credited - 0.02) < 1e-9,
        str(pays2[0].minutes_credited if pays2 else None),
    )

    # Withdrawal resets floor; next deposit above it credits fully.
    native._fetch_balance_wei = lambda: int(0.0010 * 1e18)  # type: ignore[method-assign]
    check("native withdrawal resets floor", native.poll_once() == [])
    check("native floor reset", native._floor_wei == int(0.0010 * 1e18))

    # Restart persistence: new watcher on same state file does not re-credit.
    native2 = NativeBalanceWatcher(
        address=cfg.test_watch_address,
        rpc_urls=cfg.test_rpc_urls,
        price_usd_per_minute=7.0,
        eth_per_minute=0.01,
        state_path=native_state,
        on_payments=lambda _p: None,
    )
    native2._fetch_balance_wei = lambda: int(0.0010 * 1e18)  # type: ignore[method-assign]
    check("native no re-credit after restart", native2.poll_once() == [])

    # Live: the address really holds ~0.0018 Sepolia ETH (matches the
    # diagnostic; proves the watcher reads the same balance the user sees).
    native_live = NativeBalanceWatcher(
        address=cfg.test_watch_address,
        rpc_urls=cfg.test_rpc_urls,
        price_usd_per_minute=7.0,
        eth_per_minute=0.01,
        state_path=tmpdir / "live_state.json",
        on_payments=lambda _p: None,
    )
    live_wei = native_live._fetch_balance_wei()
    check("native live balance > 0", live_wei > 0, f"{live_wei / 1e18:.6f} ETH")

    # 7. Offscreen full-window construction (starts watcher; closed immediately)
    from PyQt6.QtWidgets import QApplication

    from services.metering import Meter as Meter2
    from ui.main_window import MainWindow
    from ui.theme import DARK_QSS

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(DARK_QSS)
    window = MainWindow(cfg, Meter2(price_usd_per_minute=7.0, balance_path=tmpdir / "b2.json"))
    window.show()
    app.processEvents()
    check("main window constructed", True)
    check(
        "three watchers when test source enabled",
        len(window._watchers) == 3,
        f"{len(window._watchers)} watchers",
    )

    # 8. Payment signal -> meter credit -> UI (no network involved)
    window._meter.credit_minutes(0.0)
    fake_payment = payment_mod.Payment(
        tx_hash="0x" + "ef" * 32,
        log_index=0,
        block_number=123,
        amount_usdt=7.0,
        minutes_credited=1.0,
        detected_at="2026-08-07T00:00:00+00:00",
    )
    window._bridge.payments_detected.emit([fake_payment])
    app.processEvents()
    check(
        "payment credits meter",
        abs(window._meter.balance_minutes - 1.0) < 1e-9,
        str(window._meter.balance_minutes),
    )
    check(
        "payment visible in UI",
        "Payment received" in window._payment_widget._watch_label.text(),
        "",
    )

    # 9. Meter tick: no debit while idle; debit + auto-stop when running
    before = window._meter.balance_minutes
    window._on_meter_tick()  # widget not running -> no debit
    check("no debit while idle", abs(window._meter.balance_minutes - before) < 1e-9)

    window._audio_widget._running = True  # simulate an active session
    window._on_meter_tick()
    check(
        "debit while running",
        abs(window._meter.balance_minutes - (before - 1 / 60)) < 1e-9,
    )

    # Drain to zero: exactly 60 ticks of 1 minute remaining -> stop fires.
    with window._meter._lock:
        window._meter._balance_minutes = 0.0  # hard reset (credit_minutes(0) is a no-op)
    window._meter.credit_minutes(1.0)
    for _ in range(60):
        window._on_meter_tick()
    check("auto-stop on zero balance", not window._audio_widget.is_running(), "")

    window.close()
    app.processEvents()
    check("main window closed cleanly", True)

    print()
    if FAILURES:
        print(f"VERIFY FAILED: {len(FAILURES)} failing check(s): {FAILURES}")
        return 1
    print("VERIFY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
