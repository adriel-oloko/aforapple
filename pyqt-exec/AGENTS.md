# AGENTS.md (pyqt-exec)

PyQt6 desktop app: a port of the Next.js "Live Editor" (aforapple) camera -> WebRTC -> fal.ai Lucy 2.5 realtime video, plus a live voice-clone pipeline (mic -> AssemblyAI STT -> Fish Audio TTS -> speakers). This is NOT a web app. There is no server, no browser, no proxy routes. The Python process is the trusted environment: API keys are read directly from a local `.env`.

This directory lives inside the aforapple Next.js repo (repo root: /mnt/d/video-stuff). The root AGENTS.md's `nextjs-agent-rules` block and its Next.js route instructions do NOT apply here. The routes in `app/api/*` are not used by this app.

## Non-negotiables

- Never read, print, or commit `.env` or any API key. Keys stay out of version control.
- Never fabricate test results. Full runs need real keys (FAL_KEY, ASSEMBLYAI_API_KEY, FISH_API_KEY) and real audio/video hardware, and the GUI must run on the Windows side. WSL can only do import/syntax checks, not a full GUI smoke test.
- Verify claims about Fish Audio / AssemblyAI behavior before writing code against them. Their APIs drift; the README and code comments record what was verified. When you verify something new on a live call, patch this file.
- Do not "clean up" the defensive patches (win_ifaddr_patch, config cp1252 fallback). Each exists because a real crash was observed. See Failure modes.

## Commands

- Run (Windows): `run.bat` -> activates `d:\video-stuff\.venv` (Python 3.13.14) and runs `main.py`. From WSL: `/mnt/d/video-stuff/.venv/Scripts/python.exe main.py` will not show a GUI; run on Windows.
- Install deps into that venv: `pip install -r requirements.txt`.
- Fillers: `python generate_fillers.py --profile <Name>` (case-insensitive match), `--force` to regenerate, `--list` to list profiles. Needs FISH_API_KEY.
- Logs: every run writes `logs/session-YYYYMMDD-HHMMSS.log` and mirrors to stdout. Read the latest log first when debugging a live issue.

## Architecture map

- `main.py` entrypoint. MUST call `services.win_ifaddr_patch.apply()` before any RTCPeerConnection exists (it monkeypatches ifaddr; no-op off Windows). Runs Qt + asyncio together via qasync; WebRTC signaling and peer connection are async-native.
- `ui/main_window.py` composes `live_editor_widget.py` (video) + `advanced_audio_widget.py` (voice clone). `ui/text_block_list.py` renders per-phrase transcript blocks; `ui/theme.py` holds the shared dark QSS.
- `services/fal_client.py` WebRTC signaling + reference-image upload to fal.ai (Lucy 2.5 realtime, video only; no audio over the WebRTC session).
- Voice pipeline threading (three independent worker threads + the mic callback):
  - `services/assemblyai_stream.py`: realtime STT, its own thread. Releases phrase-sized chunks (sentence end, clause pause, or 5-word tail cap). Released chunks are NEVER revised, by design (latency trade-off).
  - `services/fish_audio_client.py`: TTS. Playback is strictly in-order and never overlapped; requests run with parallel lookahead (`FISH_TTS_MAX_PARALLEL=3`, dispatch order preserved). Tries WebSocket `/v1/tts/live` first, falls back to HTTP for the rest of the session if the backend rejects WebSocket. Auto-filler injects pre-recorded filler WAVs on long gaps.
  - `services/mic_capture.py` + `audio_devices.py` + `camera_devices.py`: hardware enumeration/capture.
- GUI-thread rule: worker-thread callbacks may NOT touch widgets. Every worker callback in `advanced_audio_widget.py` is a `_bg_thread` trampoline that re-emits a `pyqtSignal`; the connected `_main_thread` slot does the widget work. Keep new callbacks on this pattern.
- `services/config.py` reads `.env`; `services/session_log.py` owns the logger (console + file).

## Config and env knobs (defaults that matter)

- `FAL_KEY`, `ASSEMBLYAI_API_KEY`, `FISH_API_KEY` in `.env` at project root. Video needs only FAL; voice needs AssemblyAI + Fish.
- `FISH_TTS_BACKEND` defaults to `s2.1-pro` (PAID). Do not switch to `-free` variants for demos: free tier has no TTFA/DPA guarantees and measured 6-16s per phrase. Opting back in is an explicit user decision.
- `FISH_TTS_TRANSPORT` default `websocket`; `FISH_TTS_LATENCY` default `low` (the enum is only low/balanced/normal, low is the floor, there is nothing lower); `FISH_TTS_CHUNK_LENGTH=100`, `FISH_TTS_MIN_CHUNK_LENGTH=25`.
- STT: `ASSEMBLYAI_MAX_TAIL_WORDS=5`, `ASSEMBLYAI_MIN_CLAUSE_WORDS=2`, `ASSEMBLYAI_MIN_TURN_SILENCE_MS=250`, `ASSEMBLYAI_MAX_TURN_SILENCE_MS=800`. Model is `universal-3-5-pro` (not `u3-rt-pro`). `format_turns` is intentionally NOT passed: it is a no-op on U3.5 Pro; do not re-add it.
- `FILLER_LONG_GAP_SECONDS=1.2` splits fillers into single-word clips (below) vs hedging phrases (at/above).
- Sub-second end-to-end turnaround is an aggressive upper bound, not a guarantee; per-phrase STT is ~30ms, TTS ~2.5-3s on the paid backend in recent logs.

## Voice profiles

- Each `profiles/<Name>/` folder is one voice. Canonical folder is `profiles/`; `temp-profiles/` is scratch.
- Reference audio = the first audio file whose stem is NOT a known filler slug (`.wav/.mp3/.m4a/.flac`); transcript = same-stem `.txt`, exact words required for instant-clone mode.
- Filler clips are discovered by filename: `services/voice_profiles.filler_slug(text)` -> `so_ummm.wav`, etc. Adding a filler to `FILLER_TEXTS`/`FILLER_PHRASES` changes the slug contract; regenerate with `generate_fillers.py --force`.
- `profile.json` stores `reference_id`, the Fish Audio persistent model created once via `POST /model` (`train_mode=fast`). It eliminates ~2-4s per-phrase speaker embedding; profiles without it fall back to sending raw reference bytes (slower). Never hand-edit a reference_id; let `create_fish_model` write it.

## Failure modes and anti-patterns

- ifaddr crash on Windows: any adapter whose internal name is not valid UTF-8 (VPN clients, virtual adapters) makes ICE gathering raise, which surfaces as "fal connection failed". `win_ifaddr_patch.apply()` fixes this by decoding leniently. Removing the call or the patch breaks machines with such adapters.
- `.env` saved as cp1252/ANSI (Notepad smart quotes) makes python-dotenv raise `UnicodeDecodeError: 'utf-8' codec can't decode byte 0x85`. `config.py` re-encodes in memory. Keep that handling; do not "simplify" it.
- aiortc pin is load-bearing: `aiortc>=1.14,<1.16` because aiortc 1.9 pins `av<13`, which has no prebuilt wheels for Python 3.13 and forces a full FFmpeg source compile that fails on Windows (`Cannot open include file: libavutil/mathematics.h`). Do not downgrade aiortc.
- Fish `latency` accepts only low/balanced/normal. Do not invent a more aggressive value.
- Camera picker probes indices 0-7 and can only name them "Camera N". Selection applies at the next session start.
- Do not enumerate audio devices from the UI thread (sounddevice can block); the existing helpers run in their own context.
- Video input/output boxes are fixed 480x280; Escape (window-level shortcut) exits the expanded view. Preserve that shortcut.
- Session logs are the debugging record; latency numbers in the log distinguish STT model turnaround (since last audio push) from user speaking time. Do not conflate them in future metrics.

## Docs of record

- README.md in this directory (architecture, latency tuning, limitations). Update it alongside any behavioral change.
- Root-level FISH-AUDIO.md and ASSEMBLY-AI.md (repo root) are the API references this code was verified against.
