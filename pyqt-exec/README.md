# Live Editor (PyQt)

A PyQt6 desktop port of the Next.js "Live Editor" app
([adriel-oloko/aforapple](https://github.com/adriel-oloko/aforapple)),
plus a new real-time voice-cloning pipeline.

## What's in here

**Live Editor** (top section) — direct port of the original React app:
webcam → WebRTC → fal.ai's `decart/lucy-2-5/realtime` model, with a live
edit-instruction prompt and optional reference-image upload for character
swap / virtual try-on.

**Advanced · Audio (Voice Clone)** (collapsible section below it) — new
functionality: your mic → AssemblyAI real-time speech-to-text → Fish Audio
`s2.1-pro` voice cloning → your chosen speaker/output device, so whatever
you say comes back out in the cloned voice, live.

## Why this differs from the original architecturally

The Next.js app ran three small server routes whose *only* job was
keeping `FAL_KEY` off the browser (mint a short-lived token, proxy
inference, upload images) — a browser is untrusted, so secrets can't
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
   - **Windows / macOS**: nothing extra — PortAudio ships in the wheel.
   - **Linux**: `sudo apt install libportaudio2` (Debian/Ubuntu) or the
     equivalent for your distro.

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **API keys** — copy `.env.example` to `.env` and fill in:
   - `FAL_KEY` — [fal.ai dashboard](https://fal.ai/dashboard/keys) (for the video model)
   - `ASSEMBLYAI_API_KEY` — [AssemblyAI dashboard](https://www.assemblyai.com/dashboard) (for live speech-to-text)
   - `FISH_API_KEY` — [Fish Audio](https://fish.audio/) account settings (for voice-cloned TTS)

   You only need `FAL_KEY` to use the Live Editor section, and only
   `ASSEMBLYAI_API_KEY` + `FISH_API_KEY` for the Advanced Audio section.

5. **Run it:**
   ```bash
   python main.py
   ```

## Using the Advanced Audio section

1. Expand "Advanced · Audio (Voice Clone)".
2. **Choose audio file** — pick a clean 10–30 second clip of the voice
   you want to clone (per Fish Audio's own guidance for best quality).
3. **Reference transcript** — type exactly what's said in that clip.
   Fish Audio's on-the-fly cloning needs the transcript alongside the
   audio; there's no separate training step.
4. Pick your **input** (mic) and **output** (playback) devices from the
   dropdowns. The output list includes anything your OS exposes,
   including virtual audio cables like VB-Cable if installed — pick
   your own speakers to hear it yourself, or a virtual cable if you
   want another app to receive it as if it were a mic.
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
    audio_devices.py           # input/output device enumeration
    mic_capture.py              # mic -> PCM chunk callback
    assemblyai_stream.py        # real-time STT wrapper (its own thread)
    fish_audio_client.py        # cloned-voice TTS worker (its own thread)
  requirements.txt
  .env.example
```

## Known limitations / things to revisit

- Utterances are processed **one at a time, in order** — if you talk
  faster than the cloned voice can keep up, sentences queue rather
  than overlap. Overlapping cloned speech would just sound like noise,
  so this is deliberate, but it means there can be a lag if you talk
  continuously.
- The webcam defaults to device index `0`. If you have multiple
  cameras, that's currently hardcoded in `live_editor_widget.py`
  (`cv2.VideoCapture(0)`) — happy to add a camera picker if useful.
- No audio is currently sent over the WebRTC video session — Lucy 2.5
  is video-only in this app, matching the original.
