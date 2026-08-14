# Live Editor (PyQt)

A PyQt6 desktop port of the Next.js "Live Editor" app
([adriel-oloko/aforapple](https://github.com/adriel-oloko/aforapple)),
plus a new real-time voice-cloning pipeline.

## What's in here

**Live Editor** (top section) â€” direct port of the original React app:
camera â†’ WebRTC â†’ fal.ai's `decart/lucy-2-5/realtime` model, with a camera
picker, a live edit-instruction prompt, and optional reference-image
upload for character swap / virtual try-on. Both the input and output
video boxes are fixed at 480Ã—280 so neither view can push the other
around as real frames start rendering. Clicking "Expand view" shows the
output full-screen; **Escape** returns it to normal â€” this is a proper
Qt window-level shortcut, so it works regardless of which widget has
focus.

**Advanced Â· Audio (Voice Clone)** (collapsible section below it) â€” new
functionality: your mic â†’ AssemblyAI real-time speech-to-text â†’ Fish Audio
`s2.1-pro` voice cloning â†’ your chosen speaker/output device, so whatever
you say comes back out in the cloned voice, live.

**Session log** â€” every run writes a timestamped log to
`logs/session-YYYYMMDD-HHMMSS.log`, and the same lines print to the
console, mirroring what `npm run dev` gives you in PowerShell. Session
start/stop, camera selection, WebRTC signaling steps (token request,
ICE servers, offer/answer), reference uploads, voice-clone pipeline
start/stop, and every finalized transcript sent to Fish Audio are all
logged. Errors are logged in addition to being shown in the UI.

## Why this differs from the original architecturally

The Next.js app ran three small server routes whose *only* job was
keeping `FAL_KEY` off the browser (mint a short-lived token, proxy
inference, upload images) â€” a browser is untrusted, so secrets can't
live there. A desktop app has no such boundary: the Python process
*is* the trusted environment, so all three keys (`FAL_KEY`,
`ASSEMBLYAI_API_KEY`, `FISH_API_KEY`) are just read directly from a
local `.env` file. Keep it out of version control regardless.

## Setup

1. **Python 3.11+** and a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   ```

2. **System audio library** (needed by `sounddevice`):
   - **Windows / macOS**: nothing extra â€” PortAudio ships in the wheel.
   - **Linux**: `sudo apt install libportaudio2` (Debian/Ubuntu) or the
     equivalent for your distro.

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **API keys** â€” copy `.env.example` to `.env` and fill in:
   - `FAL_KEY` â€” [fal.ai dashboard](https://fal.ai/dashboard/keys) (for the video model)
   - `ASSEMBLYAI_API_KEY` â€” [AssemblyAI dashboard](https://www.assemblyai.com/dashboard) (for live speech-to-text)
   - `FISH_API_KEY` â€” [Fish Audio](https://fish.audio/) account settings (for voice-cloned TTS)

   You only need `FAL_KEY` to use the Live Editor section, and only
   `ASSEMBLYAI_API_KEY` + `FISH_API_KEY` for the Advanced Audio section.

5. **Run it:**
   ```bash
   python main.py
   ```

## Using the Advanced Audio section

1. Expand "Advanced Â· Audio (Voice Clone)".
2. **Choose audio file** â€” pick a clean 10â€“30 second clip of the voice
   you want to clone (per Fish Audio's own guidance for best quality).
3. **Reference transcript** â€” type exactly what's said in that clip.
   Fish Audio's on-the-fly cloning needs the transcript alongside the
   audio; there's no separate training step.
4. Pick your **input** (mic) and **output** (playback) devices from the
   dropdowns. The output list includes anything your OS exposes,
   including virtual audio cables like VB-Cable if installed -- pick
   your own speakers to hear it yourself, a virtual cable if you want
   another app to receive it as if it were a mic, or use the second
   **"Also output to (optional)"** dropdown to send to both at once.
5. **Start voice clone.** Speech is transcribed in real time; each
   finalized sentence (not the live partial text) is sent to Fish
   Audio and spoken back in the cloned voice, one sentence at a time.

## Project layout

```
pyqt-exec/
  main.py                    # entrypoint; bridges asyncio + Qt via qasync
  ui/
    theme.py                 # shared dark QSS (black bg, hairline borders)
    status_badge.py           # small status-dot pill widget
    live_editor_widget.py     # port of LiveRealtimeEditor.tsx
    advanced_audio_widget.py  # new voice-clone section
    main_window.py             # composes both sections
  services/
    config.py                 # reads .env
    fal_client.py              # WebRTC signaling + reference image upload
    camera_devices.py          # camera enumeration (probes indices 0-7)
    audio_devices.py           # input/output audio device enumeration
    mic_capture.py              # mic -> PCM chunk callback
    assemblyai_stream.py        # real-time STT wrapper (its own thread)
    fish_audio_client.py        # cloned-voice TTS worker (its own thread)
    session_log.py               # console + logs/session-*.log writer
  requirements.txt
  .env.example
  logs/                       # created on first run, one file per session
```

## Latency tuning

The Advanced Audio pipeline defaults to low-latency settings that are useful
for live demos:

- `FISH_TTS_BACKEND=s2.1-pro` (paid, not `-free` -- see below)
- `FISH_TTS_LATENCY=low`, `FISH_TTS_CHUNK_LENGTH=100`, and
  `FISH_TTS_MIN_CHUNK_LENGTH=25`
- `FISH_TTS_TRANSPORT=websocket` with HTTP fallback
- `FISH_TTS_MAX_PARALLEL=3` for ordered request lookahead
- **Persistent voice model**: reference audio is uploaded once to Fish Audio
  via `POST /model` (`train_mode=fast`). The model `_id` is stored in the
  profile's `profile.json` and used as `reference_id` on every TTS request,
  eliminating the per-phrase speaker-embedding computation (~2-4s). Profiles
  without a persistent model fall back to instant cloning (sending raw
  reference audio bytes, with higher latency).
- AssemblyAI realtime STT model: `universal-3-5-pro` (the current realtime
  flagship; the older `u3-rt-pro` model was in use previously)
- `ASSEMBLYAI_MAX_TAIL_WORDS=5`, `ASSEMBLYAI_MIN_CLAUSE_WORDS=2`
- `ASSEMBLYAI_MIN_TURN_SILENCE_MS=250`, `ASSEMBLYAI_MAX_TURN_SILENCE_MS=800`
- `FILLER_LONG_GAP_SECONDS=1.2` -- auto-filler picks a single-word clip
  (e.g. "Ummm") below this gap length and a longer hedging-phrase clip
  (e.g. "So um, like, honestly, it's kind of a whole thing right now.")
  at or above it, randomly among whichever clips the active profile has
  in that tier.

Each session log now prints rolling STT and TTS current/p50/p95 latency
(labelled with the active `FISH_TTS_BACKEND` and transport) so you can tell
whether Fish generation or AssemblyAI turn timing is dominating. The STT
number reflects model turnaround since audio was last sent for the current
phrase, not the time the user spent speaking before that phrase boundary
was reached -- those used to be conflated, which made STT look far slower
than it actually was on slower speakers or longer turns.

Per Fish Audio's docs: the `latency` parameter only has three tiers --
`low` / `balanced` / `normal` (best quality, default) -- so `low` is already
the most aggressive setting available; there is nothing more aggressive to
switch to for `s2.1-pro`. Separately, `-free` backends (e.g.
`s2.1-pro-free`) are documented as lacking TTFA (time-to-first-audio) and
DPA (delivery performance assurance) guarantees -- no explicit queue-priority
mechanism behind paid traffic is documented, but in testing this was the
single largest source of end-to-end latency: `s2.1-pro-free` measured 6-16s
per phrase in the session logs. `FISH_TTS_BACKEND` now defaults to the paid
`s2.1-pro` for that reason; set it back to `s2.1-pro-free` to opt back into
the free tier's latency variance (e.g. for casual/no-cost testing).

Even with all of the above, sub-second end-to-end turnaround (STT finalize +
network + TTS first-audio + playback start) is an aggressive target -- treat
it as an upper bound to chase per-phrase, not a guarantee. Real conversational
audio, longer phrases, and network variance will push some phrases past it
even on the paid backend.

## Known limitations / things to revisit

- TTS requests are fetched with ordered parallel lookahead: playback stays
  one phrase at a time so the cloned voice does not overlap itself, but the
  next Fish Audio request can start while the previous phrase is still
  streaming or playing. By default it tries Fish Audio WebSocket streaming
  first, then falls back to HTTP if the selected backend rejects WebSocket.
- The camera picker probes device indices 0â€“7 and lists whichever ones
  actually open â€” most webcam APIs don't expose friendlier names than
  that, so cameras show up as "Camera 0", "Camera 1", etc. Selecting a
  different camera takes effect the next time you start a session.
- No audio is currently sent over the WebRTC video session â€” Lucy 2.5
  is video-only in this app, matching the original.

