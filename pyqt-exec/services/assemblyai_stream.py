"""Real-time speech-to-text via AssemblyAI's Universal-Streaming v3 client.

We only care about *finalized* turns (end_of_turn=True) -- partial/interim
text is shown live in the transcript log for feedback, but only finalized
text gets pushed downstream to Fish Audio for cloned-voice synthesis, so we
don't end up re-synthesizing a sentence that's still being revised.

StreamingClient.stream() is a blocking call fed by a generator, so this
class runs the whole session on its own background thread and talks back
to the caller (the Qt main thread) purely through the two callbacks passed
into __init__ -- never touch Qt widgets directly from here.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

from assemblyai.streaming.v3 import (
    BeginEvent,
    StreamingClient,
    StreamingClientOptions,
    StreamingError,
    StreamingEvents,
    StreamingParameters,
    TerminationEvent,
    TurnEvent,
)

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 800  # 50ms of audio at 16kHz


class AssemblyAIStreamer:
    """
    on_partial_text(text): called for in-progress (not yet finalized) turns
    on_final_text(text): called once a turn is finalized -- feed this to Fish Audio
    on_status(status): "connecting" | "listening" | "error" | "stopped"
    on_error(message)
    """

    def __init__(
        self,
        api_key: str,
        on_partial_text: Callable[[str], None],
        on_final_text: Callable[[str], None],
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
    ):
        self._api_key = api_key
        self._on_partial_text = on_partial_text
        self._on_final_text = on_final_text
        self._on_status = on_status
        self._on_error = on_error

        self._audio_queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._client: Optional[StreamingClient] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = False

    # ── public API, called from the capture thread / main thread ──────────

    def push_audio(self, pcm_bytes: bytes):
        """Feed raw 16kHz mono 16-bit PCM chunks in. Called from the mic
        capture callback (its own thread, per sounddevice's model)."""
        if not self._stopping:
            self._audio_queue.put(pcm_bytes)

    def start(self):
        if self._thread is not None:
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping = True
        self._audio_queue.put(None)  # unblock the generator
        if self._client:
            try:
                self._client.disconnect(terminate=True)
            except Exception:
                pass
        self._thread = None

    # ── internal ────────────────────────────────────────────────────────────

    def _audio_generator(self):
        while not self._stopping:
            chunk = self._audio_queue.get()
            if chunk is None:
                break
            yield chunk

    def _run(self):
        try:
            self._on_status("connecting")
            client = StreamingClient(
                StreamingClientOptions(api_key=self._api_key)
            )
            self._client = client

            client.on(StreamingEvents.Begin, self._handle_begin)
            client.on(StreamingEvents.Turn, self._handle_turn)
            client.on(StreamingEvents.Termination, self._handle_termination)
            client.on(StreamingEvents.Error, self._handle_error)

            client.connect(
                StreamingParameters(
                    sample_rate=SAMPLE_RATE,
                    format_turns=True,
                )
            )
            self._on_status("listening")
            client.stream(self._audio_generator())
        except Exception as err:  # noqa: BLE001
            self._on_error(str(err))
        finally:
            self._on_status("stopped")

    def _handle_begin(self, client, event: BeginEvent):  # noqa: ARG002
        pass

    def _handle_turn(self, client, event: TurnEvent):  # noqa: ARG002
        text = (event.transcript or "").strip()
        if not text:
            return
        if event.end_of_turn:
            self._on_final_text(text)
        else:
            self._on_partial_text(text)

    def _handle_termination(self, client, event: TerminationEvent):  # noqa: ARG002
        self._on_status("stopped")

    def _handle_error(self, client, error: StreamingError):  # noqa: ARG002
        self._on_error(str(error))
