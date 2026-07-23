"""Real-time voice-cloned text-to-speech via Fish Audio's s2.1-pro model.

Runs its own worker thread that pulls finalized utterances (from
AssemblyAIStreamer.on_final_text) off a FIFO queue and speaks them one at
a time, in the cloned voice, streaming PCM audio straight to the chosen
output device as it's generated -- so playback starts before the whole
utterance has finished synthesizing.

Utterances are deliberately serialized (not overlapped): playing two
cloned-voice sentences on top of each other would come out as noise, so
if the user talks faster than TTS can keep up, later sentences simply
queue up and play in order.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
from fish_audio_sdk import ReferenceAudio, Session, TTSRequest

MODEL_HEADER = "s2.1-pro"
OUTPUT_SAMPLE_RATE = 44100  # Fish Audio's default PCM output rate


class FishVoiceCloneSpeaker:
    """
    on_status(status): "idle" | "speaking" | "error"
    on_error(message)
    """

    def __init__(
        self,
        api_key: str,
        reference_audio_bytes: bytes,
        reference_transcript: str,
        output_device_index: Optional[int],
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
    ):
        self._session = Session(api_key)
        self._reference = ReferenceAudio(audio=reference_audio_bytes, text=reference_transcript)
        self._output_device_index = output_device_index
        self._on_status = on_status
        self._on_error = on_error

        self._utterance_queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stopping = False

    # ── public API ──────────────────────────────────────────────────────

    def enqueue_text(self, text: str):
        if not self._stopping and text.strip():
            self._utterance_queue.put(text.strip())

    def start(self):
        if self._thread is not None:
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping = True
        self._utterance_queue.put(None)
        self._thread = None

    # ── internal ────────────────────────────────────────────────────────

    def _run(self):
        self._on_status("idle")
        while not self._stopping:
            text = self._utterance_queue.get()
            if text is None:
                break
            self._speak(text)
        self._on_status("idle")

    def _speak(self, text: str):
        try:
            self._on_status("speaking")
            request = TTSRequest(
                text=text,
                format="pcm",
                sample_rate=OUTPUT_SAMPLE_RATE,
                references=[self._reference],
                latency="balanced",
            )

            with sd.OutputStream(
                samplerate=OUTPUT_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                device=self._output_device_index,
            ) as out_stream:
                for chunk in self._session.tts(request):
                    if self._stopping:
                        break
                    if not chunk:
                        continue
                    pcm = np.frombuffer(chunk, dtype=np.int16)
                    out_stream.write(pcm)
        except Exception as err:  # noqa: BLE001
            self._on_error(str(err))
        finally:
            self._on_status("idle")
