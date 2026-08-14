"""Real-time speech-to-text via AssemblyAI's Universal-Streaming v3 client.

Text reaches Fish Audio in phrase-sized chunks while the user speaks, not only
once a whole turn is finalized. AssemblyAI's transcript on each Turn event is
cumulative for the current turn, so this class tracks how much text has already
been released and sends only the new tail once it reaches a safe boundary.

Released chunks are never revised after they are sent to Fish Audio. That is the
latency trade-off: a rare STT correction inside an already-spoken chunk will not
be mirrored in the cloned voice, but phrase boundaries at punctuation and short
word-count fallbacks keep the voice conversion feeling live.
"""

from __future__ import annotations

import os
import queue
import re
import statistics
import threading
import time
from typing import Callable, Optional

from assemblyai.streaming.v3 import (
    BeginEvent,
    SpeechModel,
    StreamingClient,
    StreamingClientOptions,
    StreamingError,
    StreamingEvents,
    StreamingMode,
    StreamingParameters,
    TerminationEvent,
    TurnEvent,
)

from services.session_log import get_logger

log = get_logger()

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 800  # 50ms of audio at 16kHz

_SENTENCE_END_RE = re.compile(r"[.!?]+[\"')\]]?\s*$")
_CLAUSE_PAUSE_RE = re.compile(r"[,;:\-]\s*$")
_MAX_TAIL_WORDS_BEFORE_FORCED_RELEASE = int(os.getenv("ASSEMBLYAI_MAX_TAIL_WORDS", "5"))
_MIN_TAIL_WORDS_FOR_CLAUSE_RELEASE = int(os.getenv("ASSEMBLYAI_MIN_CLAUSE_WORDS", "2"))
# Lowered from 400/1200 -> 250/800 per the latency review: fires end-of-turn
# faster, at some risk of cutting off a speaker who pauses mid-thought. Tune
# back up via the env vars if turns are ending prematurely. AssemblyAI clamps
# min_turn_silence to >=50ms; 250ms leaves margin above that floor for normal
# conversational speech.
_MIN_TURN_SILENCE_MS = int(os.getenv("ASSEMBLYAI_MIN_TURN_SILENCE_MS", "250"))
_MAX_TURN_SILENCE_MS = int(os.getenv("ASSEMBLYAI_MAX_TURN_SILENCE_MS", "800"))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = round((len(ordered) - 1) * percentile)
    return ordered[idx]


def _latency_summary(values: list[float]) -> tuple[float, float]:
    return statistics.median(values), _percentile(values, 0.95)


def _is_silent_punctuation(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return not any(ch.isalnum() for ch in stripped)


class AssemblyAIStreamer:
    """
    on_partial_text(text): full in-progress transcript of the current turn.
    on_final_text(text, chunk_id): phrase-sized text ready for Fish Audio.
    on_status(status): "connecting" | "listening" | "error" | "stopped"
    on_error(message)
    """

    def __init__(
        self,
        api_key: str,
        on_partial_text: Callable[[str], None],
        on_final_text: Callable[[str, str], None],
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
        on_pending_count: Optional[Callable[[int], None]] = None,
        on_latency: Optional[Callable[[str, float], None]] = None,
    ):
        self._api_key = api_key
        self._on_partial_text = on_partial_text
        self._on_final_text = on_final_text
        self._on_status = on_status
        self._on_error = on_error
        self._on_pending_count = on_pending_count
        self._on_latency = on_latency

        self._audio_queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._client: Optional[StreamingClient] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = False

        self._released_len = 0
        self._pending = 0
        self._chunk_seq = 0
        # Timestamp of the most recently pushed audio chunk. Used (rather than
        # a turn-start timestamp) so latency measures "model turnaround since
        # audio was last sent", not "how long the user has been talking".
        # Conflating the two used to make a slow speaker look like a slow model.
        self._last_audio_push_ts: Optional[float] = None

        self._stt_latencies: list[float] = []
        self._stt_latency_lock = threading.Lock()

    # public API

    def push_audio(self, pcm_bytes: bytes):  # noqa: D401
        """Feed raw 16kHz mono 16-bit PCM chunks into the streamer."""
        if not self._stopping:
            self._last_audio_push_ts = time.perf_counter()
            self._audio_queue.put(pcm_bytes)

    def ack_played(self):
        """Decrement pending count when Fish Audio finishes playback."""
        if self._pending > 0:
            self._pending -= 1
            if self._on_pending_count:
                try:
                    self._on_pending_count(self._pending)
                except Exception:  # noqa: BLE001
                    pass

    def start(self):
        if self._thread is not None:
            return
        self._stopping = False
        self._released_len = 0
        self._last_audio_push_ts = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping = True
        self._audio_queue.put(None)
        if self._client:
            try:
                self._client.disconnect(terminate=True)
            except Exception:
                pass
        self._thread = None

    # internal

    def _audio_generator(self):
        while not self._stopping:
            chunk = self._audio_queue.get()
            if chunk is None:
                break
            yield chunk

    def _run(self):
        try:
            self._on_status("connecting")
            client = StreamingClient(StreamingClientOptions(api_key=self._api_key))
            self._client = client

            client.on(StreamingEvents.Begin, self._handle_begin)
            client.on(StreamingEvents.Turn, self._handle_turn)
            client.on(StreamingEvents.Termination, self._handle_termination)
            client.on(StreamingEvents.Error, self._handle_error)

            log.info(
                "AssemblyAI streaming start model=%s mode=%s min_turn_silence=%dms "
                "max_turn_silence=%dms max_tail_words=%d min_clause_words=%d",
                SpeechModel.universal_3_5_pro.value,
                StreamingMode.min_latency.value,
                _MIN_TURN_SILENCE_MS,
                _MAX_TURN_SILENCE_MS,
                _MAX_TAIL_WORDS_BEFORE_FORCED_RELEASE,
                _MIN_TAIL_WORDS_FOR_CLAUSE_RELEASE,
            )
            # NOTE: universal-3-5-pro (not u3-rt-pro) is the current realtime
            # flagship -- it gets the latest turn-detection/latency tuning.
            # format_turns is intentionally omitted: it's a knob for the older
            # universal-streaming-* model family. On U3.5 Pro, formatting
            # always tracks end_of_turn and the param has no effect, so
            # passing it is just a leftover from pre-U3.5 docs.
            client.connect(
                StreamingParameters(
                    sample_rate=SAMPLE_RATE,
                    speech_model=SpeechModel.universal_3_5_pro,
                    mode=StreamingMode.min_latency,
                    min_turn_silence=_MIN_TURN_SILENCE_MS,
                    max_turn_silence=_MAX_TURN_SILENCE_MS,
                )
            )
            self._on_status("listening")
            client.stream(self._audio_generator())
        except Exception as err:  # noqa: BLE001
            self._on_error(str(err))
        finally:
            self._on_status("stopped")

    def _handle_begin(self, client, event: BeginEvent):  # noqa: ARG002
        self._released_len = 0
        self._last_audio_push_ts = None
        if self._pending != 0:
            self._pending = 0
            if self._on_pending_count:
                try:
                    self._on_pending_count(0)
                except Exception:  # noqa: BLE001
                    pass

    def _handle_turn(self, client, event: TurnEvent):  # noqa: ARG002
        text = (event.transcript or "").strip()
        if text and _is_silent_punctuation(text):
            self._on_partial_text(text)
            if event.end_of_turn:
                self._released_len = 0
            return
        if not text:
            return

        self._on_partial_text(text)
        unreleased = text[self._released_len :].lstrip()
        if not unreleased:
            if event.end_of_turn:
                self._released_len = 0
            return

        if event.end_of_turn:
            self._release_chunk(unreleased, self._last_audio_push_ts)
            self._released_len = 0
            return

        chunk = self._safe_chunk_to_release(unreleased)
        if chunk is not None:
            self._release_chunk(chunk, self._last_audio_push_ts)
            self._released_len = len(text)

    def _release_chunk(self, chunk: str, stt_send_ts: Optional[float]):
        if _is_silent_punctuation(chunk):
            return

        self._chunk_seq += 1
        chunk_id = f"c{self._chunk_seq}"
        self._on_final_text(chunk, chunk_id)

        if stt_send_ts is not None:
            # Model turnaround since the most recent audio chunk was sent,
            # i.e. the STT model's own processing/network latency -- not
            # inclusive of however long the user spent talking before this
            # phrase boundary was reached. That "user speaking time" is a
            # property of speech pacing and turn-silence tuning, not of STT
            # speed, and mixing the two into one number made STT look far
            # slower than it actually is.
            latency = time.perf_counter() - stt_send_ts
            with self._stt_latency_lock:
                self._stt_latencies.append(latency)
                p50, p95 = _latency_summary(self._stt_latencies)
                count = len(self._stt_latencies)
            log.info(
                "STT latency chunk_id=%s current=%.0fms p50=%.0fms p95=%.0fms n=%d",
                chunk_id,
                latency * 1000,
                p50 * 1000,
                p95 * 1000,
                count,
            )
            if self._on_latency is not None:
                try:
                    self._on_latency(chunk, latency)
                except Exception:  # noqa: BLE001
                    pass

        self._pending += 1
        if self._on_pending_count:
            try:
                self._on_pending_count(self._pending)
            except Exception:  # noqa: BLE001
                pass

    def _safe_chunk_to_release(self, unreleased: str) -> Optional[str]:
        words = unreleased.split()

        if _SENTENCE_END_RE.search(unreleased):
            return unreleased

        if len(words) >= _MIN_TAIL_WORDS_FOR_CLAUSE_RELEASE and _CLAUSE_PAUSE_RE.search(unreleased):
            return unreleased

        if len(words) >= _MAX_TAIL_WORDS_BEFORE_FORCED_RELEASE:
            return unreleased

        return None

    def _handle_termination(self, client, event: TerminationEvent):  # noqa: ARG002
        self._on_status("stopped")

    def _handle_error(self, client, error: StreamingError):  # noqa: ARG002
        self._on_error(str(error))
