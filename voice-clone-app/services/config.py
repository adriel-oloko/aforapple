"""Loads API keys and app settings from a local .env file.

This is a desktop app, so unlike the original Next.js project there's no
browser to hide these keys from -- they're just read directly into the
Python process. Keep your .env out of version control regardless.

The .env lives next to the exe (APP_DIR). In a frozen onefile build the
first launch materializes a blank .env from the bundled .env.example so
the user has a real file to fill in.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from services.app_paths import APP_DIR, BUNDLE_DIR

_ENV_PATH = APP_DIR / ".env"

# Default BNB Smart Chain public RPC endpoints (no API key required).
# BSCScan's free tier is rate-limited hard without a key; reading the
# chain directly over public RPC keeps the app self-contained.
_DEFAULT_RPC_URLS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed2.binance.org",
    "https://bsc-dataseed3.binance.org",
    "https://bsc-rpc.publicnode.com",
]

# Hidden test credit source: Circle's official USDC on Ethereum Sepolia
# (verified against developers.circle.com + Chainlink CCIP docs). Testers
# fund with faucet tokens, so no real assets move. The whole source is
# invisible in the UI; the operator toggles it via env vars.
_SEPOLIA_USDC = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
# Ordered by what actually serves eth_getLogs on the free tier (verified
# live): tenderly + publicnode work, drpc requires a paid plan, the rest
# are flaky fallbacks. The tracker rotates through them on failure.
_DEFAULT_TEST_RPC_URLS = [
    "https://sepolia.gateway.tenderly.co",
    "https://ethereum-sepolia-rpc.publicnode.com",
    "https://rpc.sepolia.org",
    "https://ethereum-sepolia.blockpi.network/v1/rpc/public",
]


def _materialize_env_file() -> None:
    """First run of a frozen build: copy the bundled .env.example to
    APP_DIR/.env so the user has a file to fill in next to the exe."""
    if _ENV_PATH.exists():
        return
    bundled = BUNDLE_DIR / ".env.example"
    if bundled.exists() and bundled != _ENV_PATH:
        try:
            shutil.copy2(bundled, _ENV_PATH)
        except OSError:
            pass


def _load_env_file(path: Path) -> None:
    """Load the .env file, tolerating files that aren't valid UTF-8.

    python-dotenv's load_dotenv() always opens the file as utf-8. On
    Windows it's easy to end up with a .env saved as cp1252/"ANSI"
    (e.g. Notepad, or an editor that auto-"smart-quotes" a dash or
    ellipsis in a comment line) -- that raises
    UnicodeDecodeError: 'utf-8' codec can't decode byte 0x85 ...
    the moment load_dotenv() reads the file.

    To be robust to that, if the file exists but isn't valid UTF-8, we
    re-encode it to UTF-8 in memory (via stream_from_str) instead of
    letting the app crash on startup over a text-encoding mismatch.
    """
    if not path.exists():
        return

    try:
        raw = path.read_bytes()
        raw.decode("utf-8")
    except UnicodeDecodeError:
        from dotenv import load_dotenv as _load_dotenv  # noqa: F401
        from dotenv.main import DotEnv

        text = raw.decode("cp1252", errors="replace")
        DotEnv(
            dotenv_path=None,
            stream=__import__("io").StringIO(text),
            verbose=True,
            interpolate=True,
            override=False,
            encoding="utf-8",
        ).set_as_environment_variables()
        return

    load_dotenv(path)


_materialize_env_file()
_load_env_file(_ENV_PATH)


@dataclass
class Config:
    assemblyai_key: str | None
    fish_key: str | None
    fal_key: str | None
    usdt_address: str
    price_usd_per_minute: float
    poll_seconds: int
    rpc_urls: list[str] = field(default_factory=list)
    # Hidden test credit source (off unless VOICE_APP_TEST_CREDIT_ENABLED=1).
    test_credit_enabled: bool = False
    test_token_contract: str = _SEPOLIA_USDC
    test_token_decimals: int = 6  # USDC is 6 decimals on Sepolia (verified)
    test_watch_address: str = ""
    test_price_usd_per_minute: float = 7.0
    test_poll_seconds: int = 12
    test_rpc_urls: list[str] = field(default_factory=list)
    # Native testnet currency (e.g. Sepolia ETH) is also credited; ETH per
    # minute of test credit. Set to 0 to disable native watching.
    test_native_eth_per_minute: float = 0.01

    def missing_for_voice_clone(self) -> list[str]:
        missing = []
        if not self.assemblyai_key:
            missing.append("ASSEMBLYAI_API_KEY")
        if not self.fish_key:
            missing.append("FISH_API_KEY")
        return missing


def load_config() -> Config:
    rpc_urls = [
        u.strip()
        for u in (os.getenv("VOICE_APP_RPC_URLS") or "").split(",")
        if u.strip()
    ]
    if not rpc_urls:
        rpc_urls = list(_DEFAULT_RPC_URLS)

    try:
        price = float(os.getenv("VOICE_APP_PRICE_USD_PER_MINUTE", "7"))
    except ValueError:
        price = 7.0

    try:
        poll_seconds = max(5, int(os.getenv("VOICE_APP_POLL_SECONDS", "12")))
    except ValueError:
        poll_seconds = 12

    test_rpc_urls = [
        u.strip()
        for u in (os.getenv("VOICE_APP_TEST_RPC_URLS") or "").split(",")
        if u.strip()
    ]
    if not test_rpc_urls:
        test_rpc_urls = list(_DEFAULT_TEST_RPC_URLS)

    try:
        test_price = float(
            os.getenv("VOICE_APP_TEST_PRICE_USD_PER_MINUTE", str(price))
        )
    except ValueError:
        test_price = price

    try:
        test_poll = max(5, int(os.getenv("VOICE_APP_TEST_POLL_SECONDS", str(poll_seconds))))
    except ValueError:
        test_poll = poll_seconds

    try:
        test_decimals = max(0, int(os.getenv("VOICE_APP_TEST_TOKEN_DECIMALS", "6")))
    except ValueError:
        test_decimals = 6

    try:
        test_native_eth_per_minute = float(
            os.getenv("VOICE_APP_TEST_NATIVE_ETH_PER_MINUTE", "0.01")
        )
    except ValueError:
        test_native_eth_per_minute = 0.01

    return Config(
        assemblyai_key=os.getenv("ASSEMBLYAI_API_KEY") or None,
        fish_key=os.getenv("FISH_API_KEY") or None,
        fal_key=os.getenv("FAL_KEY") or None,
        usdt_address=(
            os.getenv("VOICE_APP_USDT_ADDRESS")
            or "0x217153D3CC0Ca2E72d633Af400b554d5Bb1aB5F8"
        ).strip(),
        price_usd_per_minute=price,
        poll_seconds=poll_seconds,
        rpc_urls=rpc_urls,
        test_credit_enabled=(os.getenv("VOICE_APP_TEST_CREDIT_ENABLED") or "").strip() == "1",
        test_token_contract=(
            os.getenv("VOICE_APP_TEST_TOKEN_CONTRACT") or _SEPOLIA_USDC
        ).strip(),
        test_token_decimals=test_decimals,
        test_watch_address=(
            os.getenv("VOICE_APP_TEST_WATCH_ADDRESS")
            or os.getenv("VOICE_APP_USDT_ADDRESS")
            or "0x217153D3CC0Ca2E72d633Af400b554d5Bb1aB5F8"
        ).strip(),
        test_price_usd_per_minute=test_price,
        test_poll_seconds=test_poll,
        test_rpc_urls=test_rpc_urls,
    )
