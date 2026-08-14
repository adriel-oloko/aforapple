# Voice Clone Studio

Standalone desktop app for the live voice-clone pipeline, extracted from
the Live Editor (pyqt-exec) project: **mic -> AssemblyAI (STT) -> Fish
Audio (cloned-voice TTS) -> speakers**, billed **$7.00 per minute**,
paid in advance with **USDT (BEP20) on BNB Smart Chain**.

This is a separate app from the video Live Editor: no WebRTC, no camera,
no fal.ai. Pure threading pipeline, plain PyQt6 event loop.

---

## Billing model

- The app has **one** USDT (BEP20) receiving address:
  `0x217153D3CC0Ca2E72d633Af400b554d5Bb1aB5F8`
- Users top up by sending USDT on BNB Smart Chain to that address.
- The app polls the chain (public BSC RPC, **no API key needed**) for
  `Transfer` events to the address and credits the local balance at
  **$7.00 / minute** (1 USDT = 1/7 minute).
- The meter debits 1 second of balance per second while the voice-clone
  session is active (Start pressed, Stop not yet pressed). When the
  balance hits zero the session stops automatically.
- Money lands directly in the receiving wallet. There is no smart
  contract and no forwarding step; "the system detects the payment and
  tops up the balance" is exactly what happens, on-chain -> local credit.
- The balance is stored per install in `data/balance.json` next to the
  exe. Payments are detected within ~15-30 seconds (BSC block time
  ~3s + 12s poll interval).

### Address and rate configuration

Both are read from `.env` (see `.env.example`):

| Key | Default |
| --- | --- |
| `VOICE_APP_USDT_ADDRESS` | `0x217153D3CC0Ca2E72d633Af400b554d5Bb1aB5F8` |
| `VOICE_APP_PRICE_USD_PER_MINUTE` | `7` |
| `VOICE_APP_POLL_SECONDS` | `12` |
| `VOICE_APP_RPC_URLS` | public `bsc-dataseed` + `publicnode` endpoints |

### Hidden test credit source (operator-only)

For testers, the app can also accept **free faucet tokens** instead of
real USDT. It is completely invisible in the UI — no toggle, no label,
no option. The operator enables it in `.env`:

| Key | Default |
| --- | --- |
| `VOICE_APP_TEST_CREDIT_ENABLED` | off (set to `1` to enable) |
| `VOICE_APP_TEST_TOKEN_CONTRACT` | Circle USDC on Ethereum Sepolia `0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238` |
| `VOICE_APP_TEST_TOKEN_DECIMALS` | `6` (verified on-chain via `decimals()`) |
| `VOICE_APP_TEST_WATCH_ADDRESS` | same address as production |
| `VOICE_APP_TEST_PRICE_USD_PER_MINUTE` | `7` |
| `VOICE_APP_TEST_POLL_SECONDS` | `12` |
| `VOICE_APP_TEST_RPC_URLS` | public Sepolia endpoints (tenderly, publicnode, ...) |
| `VOICE_APP_TEST_NATIVE_ETH_PER_MINUTE` | `0.01` (native testnet ETH also credits; `0` disables) |

When enabled, a second tracker watches the same address on **Ethereum
Sepolia** for USDC `Transfer` events and credits the same balance, and a
third watcher credits native **Sepolia ETH** balance increases (first
poll credits whatever is already on the address, so an existing deposit
shows up on the next launch). Testers get free testnet tokens from a
faucet — Sepolia USDC (Circle's faucet) or plain Sepolia ETH — and send
them to the address the app shows; no real assets move. Testnet credits
appear in logs tagged `[test]`/`[test-native]` and are indistinguishable
from real payments in the UI by design. The production BSC watcher and
its checkpoint are untouched (test checkpoints live in
`data/payment_state_test.json` + `data/test_native_state.json`).

Keep `VOICE_APP_TEST_CREDIT_ENABLED` unset/off for production installs.

---

## Running from source (dev)

The shared venv at `..\.venv` (Python 3.13) already has the deps; if
not, install them:

    pip install -r requirements.txt

Then, on Windows:

    run.bat

Or from a terminal:

    ..\.venv\Scripts\python.exe main.py

Required keys in `.env` (next to the app): `ASSEMBLYAI_API_KEY`,
`FISH_API_KEY`.

Voice profiles live in `profiles/<Name>/` — same layout as the original
app (reference audio + same-stem transcript + filler WAVs + optional
`profile.json` with the Fish Audio persistent model id). The bundled
Elon profile is extracted next to the exe on first run; drop new
profiles into that folder to add voices.

`generate_fillers.py --profile <Name>` regenerates the filler clips
(needs `FISH_API_KEY`).

---

## Building the single .exe

    build.bat

Produces `dist/VoiceCloneApp.exe` (single file, windowed, ~150 MB).
The build bundles `profiles/` and `.env.example`; on first run the exe
extracts them next to itself and materializes `.env` for the user to
fill in.

Verify a build with:

    dist\VoiceCloneApp.exe --self-check

...which constructs the full UI offscreen, checks paths, and does a
best-effort BSC RPC reachability check, printing `SELF-CHECK OK`.

---

## Verified behavior

- Payment detection pipeline tested against live chain data (read-only):
  `eth_getLogs` on the USDT BEP20 contract returns and decodes real
  transfers correctly over publicnode RPC; dataseed endpoints throttle
  broad log queries, so the client rotates endpoints.
- Hidden test source tested against live Sepolia: RPC reachable, on-chain
  `decimals()` returns 6 for the Circle USDC contract, and real USDC
  Transfer logs decode correctly (tenderly endpoint).
- USDT/USDC value decoding verified with synthetic logs
  (1 USDT -> 1/7 minute; 1 Sepolia USDC -> 1/7 minute, 6 decimals).
- Meter math/persistence verified (credit, 1s ticks, reload).
- Full window constructs and closes cleanly offscreen.
- A real end-to-end payment test requires sending actual USDT to the
  address; that is left to the operator. STT/TTS need real API keys and
  a mic/speakers on the Windows side.

## Logs

Every run writes `logs/session-YYYYMMDD-HHMMSS.log` next to the exe
(mirrored to stdout). Read the latest log first when debugging.
