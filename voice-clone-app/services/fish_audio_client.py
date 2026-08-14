"""Real-time voice-cloned text-to-speech through Fish Audio.

The playback contract is intentionally simple: audio is always written in
phrase order, never overlapped. The request side is more aggressive: phrase
jobs are dispatched in order but fetched in parallel, so phrase N+1 can pay
its network/model latency while phrase N is still streaming or playing.

Fish Audio's WebSocket `/v1/tts/live` endpoint is used first because it lets
us send text and force a flush immediately. If the selected backend rejects
WebSocket streaming, the speaker falls back to the regular HTTP streaming TTS
endpoint for the rest of the session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import queue
import statistics
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

import httpx
import numpy as np
import ormsgpack
import sounddevice as sd
from httpx_ws import WebSocketDisconnect, connect_ws

from services.session_log import get_logger

log = get_logger()

# NOTE: per Fish Audio's docs, the "-free" suffix models are not covered by
# TTFA (time-to-first-audio) or DPA (delivery performance assurance)
# guarantees -- i.e. latency/throughput on the free tier is not guaranteed.
# Session logs measured 6-16s per-phrase TTS latency on s2.1-pro-free, which
# is consistent with unguaranteed free-tier queueing rather than a pipeline
# problem. Defaulting to the paid "s2.1-pro" backend to get off that queue --
# this uses paid Fish Audio credit instead of the free tier, so override back
# to "s2.1-pro-free" via FISH_TTS_BACKEND if that trade-off isn't wanted.
TTS_BACKEND = os.getenv("FISH_TTS_BACKEND", "s2.1-pro")
OUTPUT_SAMPLE_RATE = 44100

_FISH_BASE_URL = os.getenv("FISH_BASE_URL", "https://api.fish.audio")
_TTS_TRANSPORT = os.getenv("FISH_TTS_TRANSPORT", "websocket").strip().lower()
# Fish Audio's `latency` enum only has three tiers -- low / balanced / normal
# (best quality, the default if unset). "low" is already the most aggressive
# tier available for s2.1-pro; there is nothing lower to opt into.
_TTS_LATENCY = os.getenv("FISH_TTS_LATENCY", "low").strip().lower()
# chunk_length: Fish Audio API valid range is 100-300 (default 300).
# Lower values reduce time-to-first-audio at some cost to prosody quality.
_TTS_CHUNK_LENGTH = int(os.getenv("FISH_TTS_CHUNK_LENGTH", "100"))
_TTS_MIN_CHUNK_LENGTH = int(os.getenv("FISH_TTS_MIN_CHUNK_LENGTH", "25"))
_MAX_PARALLEL_TTS_REQUESTS = max(1, int(os.getenv("FISH_TTS_MAX_PARALLEL", "3")))

_PHRASE_QUEUE_MAXSIZE = _MAX_PARALLEL_TTS_REQUESTS + 2
_AUDIO_QUEUE_MAXSIZE = 4

_PHRASE_END = object()
_AUTO_FILLER_GAP_SECONDS = 0.3


@dataclass
class _PhraseJob:
    text: str
    chunk_id: Optional[str]
    audio_queue: "queue.Queue[object]" = field(
        default_factory=lambda: queue.Queue(maxsize=_AUDIO_QUEUE_MAXSIZE)
    )
    cancelled: threading.Event = field(default_factory=threading.Event)
    send_ts: Optional[float] = None
    recv_ts: Optional[float] = None
    transport: str = "unknown"


@dataclass
class _FillerJob:
    pcm: np.ndarray


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = round((len(ordered) - 1) * percentile)
    return ordered[idx]


def _latency_summary(values: list[float]) -> tuple[float, float]:
    return statistics.median(values), _percentile(values, 0.95)


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
        output_device_indices: "list[Optional[int]]",
        on_status: Callable[[str], None],
        on_error: Callable[[str], None],
        on_phrase_played: Optional[Callable[[str, str, float], None]] = None,
        on_active_changed: Optional[Callable[[Optional[str], Optional[str]], None]] = None,
        on_phrase_locked: Optional[Callable[[str, bool], None]] = None,
        reference_id: Optional[str] = None,
    ):
        """
        on_phrase_played(chunk_id, text, latency_send_to_recv_sec):
            fired once per phrase when playback finishes. ``chunk_id``
            echoes the value passed to enqueue_text.
        on_active_changed(chunk_id_or_none, text_or_none):
            fired for the phrase currently being played.
        on_phrase_locked(chunk_id, locked):
            fired when a phrase has left the cancellable pending queue and
            its TTS request may already be in flight.
        reference_id:
            Fish Audio persistent model _id. When provided, the raw
            reference audio bytes are NOT sent on every request -- the
            server reuses the pre-computed speaker embedding, eliminating
            the ~2-4s per-phrase embedding cost.
        """
        self._api_key = api_key
        self._reference_audio_bytes = reference_audio_bytes
        self._reference_transcript = reference_transcript
        self._reference_id = reference_id

        seen = set()
        self._output_device_indices = []
        for idx in output_device_indices:
            if idx not in seen:
                seen.add(idx)
                self._output_device_indices.append(idx)

        self._on_status = on_status
        self._on_error = on_error
        self._on_phrase_played_cb = on_phrase_played
        self._on_active_changed_cb = on_active_changed
        self._on_phrase_locked_cb = on_phrase_locked

        self._utterance_queue: "queue.Queue[Optional[tuple[str, Optional[str]]]]" = queue.Queue()
        self._play_queue: "queue.Queue[object]" = queue.Queue(maxsize=_PHRASE_QUEUE_MAXSIZE)
        self._fetch_thread: Optional[threading.Thread] = None
        self._play_thread: Optional[threading.Thread] = None
        self._out_streams: list[sd.OutputStream] = []
        self._stopping = False

        self._tts_slots = threading.BoundedSemaphore(_MAX_PARALLEL_TTS_REQUESTS)
        self._worker_threads: set[threading.Thread] = set()
        self._worker_threads_lock = threading.Lock()

        self._cancelled_chunk_ids: set[str] = set()
        self._cancel_lock = threading.Lock()
        self._pending_cancel_all = False

        self._auto_filler_enabled = False
        self._last_phrase_end_ts: Optional[float] = None
        self._filler_play_cb: Optional[Callable[[Optional[float]], Optional[str]]] = None
        self._active_chunk_id: Optional[str] = None

        self._http_client_lock = threading.Lock()
        self._http_client = self._make_http_client(api_key)
        self._transport_lock = threading.Lock()
        self._transport = "websocket" if _TTS_TRANSPORT in {"websocket", "auto", ""} else "http"

        self._tts_latencies: list[float] = []
        self._tts_latency_lock = threading.Lock()

    # public API

    def enqueue_text(self, text: str, chunk_id: Optional[str] = None):
        """Queue a phrase for TTS.

        Pure-punctuation chunks are dropped before they can trigger a Fish
        Audio round trip.
        """
        stripped = text.strip()
        if not stripped or self._stopping:
            return
        if not any(ch.isalnum() for ch in stripped):
            log.debug("Dropping silent-punctuation chunk: %r", text)
            return
        self._utterance_queue.put((stripped, chunk_id))

    def cancel_pending(self, chunk_id: Optional[str] = None):
        """Cancel queued text that has not safely started playback.

        ``chunk_id`` cancels one text block. Without it, all waiting work is
        cancelled, preserving the old broad-cancel behavior.
        """
        if self._stopping:
            return
        with self._cancel_lock:
            if chunk_id:
                self._cancelled_chunk_ids.add(chunk_id)
            else:
                self._pending_cancel_all = True
        if chunk_id is None:
            self._drain_queue(self._utterance_queue)

    def play_filler(self, wav_path: str):
        """Queue a pre-recorded filler WAV for playback."""
        if self._stopping or not wav_path:
            return
        p = Path(wav_path)
        if not p.exists():
            log.warning("play_filler: WAV not found: %s", wav_path)
            return
        try:
            import soundfile as sf

            data, sr = sf.read(str(p), dtype="int16", always_2d=False)
            arr = self._coerce_mono_int16(data)
            if sr != OUTPUT_SAMPLE_RATE:
                arr = self._resample_int16(arr, sr, OUTPUT_SAMPLE_RATE)
            self._put_play_item(_FillerJob(arr))
        except Exception as err:  # noqa: BLE001
            self._on_error(f"play_filler failed for {wav_path}: {err}")

    def set_auto_filler(
        self,
        enabled: bool,
        filler_resolver: Optional[Callable[[Optional[float]], Optional[str]]] = None,
    ):
        self._auto_filler_enabled = enabled
        if enabled and filler_resolver is not None:
            self._filler_play_cb = filler_resolver

    def start(self):
        if self._fetch_thread is not None:
            return
        self._stopping = False
        self._pending_cancel_all = False
        with self._cancel_lock:
            self._cancelled_chunk_ids.clear()

        with self._transport_lock:
            self._transport = "websocket" if _TTS_TRANSPORT in {"websocket", "auto", ""} else "http"

        with self._http_client_lock:
            if self._http_client.is_closed:
                self._http_client = self._make_http_client(self._api_key)

        self._out_streams = []
        for device_index in self._output_device_indices:
            try:
                out_stream = sd.OutputStream(
                    samplerate=OUTPUT_SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    device=device_index,
                )
                out_stream.start()
                self._out_streams.append(out_stream)
            except Exception as err:  # noqa: BLE001
                self._on_error(f"Could not open output device {device_index}: {err}")

        if not self._out_streams:
            self._on_error("No audio output device could be opened.")
            return

        log.info(
            "Fish TTS start backend=%s transport=%s latency=%s chunk_length=%d "
            "min_chunk_length=%d parallel=%d reference_id=%s",
            TTS_BACKEND,
            self._transport,
            _TTS_LATENCY,
            _TTS_CHUNK_LENGTH,
            _TTS_MIN_CHUNK_LENGTH,
            _MAX_PARALLEL_TTS_REQUESTS,
            self._reference_id or "(instant clone)",
        )
        self._on_status("idle")
        self._fetch_thread = threading.Thread(target=self._fetch_loop, daemon=True)
        self._play_thread = threading.Thread(target=self._play_loop, daemon=True)
        self._fetch_thread.start()
        self._play_thread.start()

    def stop(self):
        self._stopping = True
        self._utterance_queue.put(None)
        self._drain_queue(self._play_queue)
        self._play_queue.put(None)

        with self._http_client_lock:
            try:
                self._http_client.close()
            except Exception:  # noqa: BLE001
                pass

        current = threading.current_thread()
        for thread in list(self._worker_threads):
            if thread is current:
                continue
            try:
                thread.join(timeout=0.2)
            except RuntimeError:
                pass

        self._fetch_thread = None
        self._play_thread = None
        for out_stream in self._out_streams:
            try:
                out_stream.stop()
                out_stream.close()
            except Exception:
                pass
        self._out_streams = []

    # internal helpers

    @staticmethod
    def _make_http_client(api_key: str) -> httpx.Client:
        return httpx.Client(
            base_url=_FISH_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "live-editor/pyqt fish-audio",
            },
            timeout=None,
        )

    @staticmethod
    def _coerce_mono_int16(data) -> np.ndarray:  # noqa: ANN001
        arr = np.asarray(data, dtype=np.int16)
        if arr.ndim == 2:
            arr = arr.mean(axis=1).astype(np.int16)
        return np.ascontiguousarray(arr)

    @staticmethod
    def _resample_int16(data: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
        ratio = target_rate / source_rate
        new_len = max(1, int(len(data) * ratio))
        return np.interp(
            np.linspace(0, len(data), new_len, endpoint=False),
            np.arange(len(data)),
            data,
        ).astype(np.int16)

    @staticmethod
    def _drain_queue(q: "queue.Queue"):
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def _put_play_item(self, item: object):
        while not self._stopping:
            try:
                self._play_queue.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def _put_job_audio(self, job: _PhraseJob, item: object, *, force: bool = False):
        while not self._stopping:
            if not force and self._is_cancelled(job.chunk_id):
                job.cancelled.set()
                return
            try:
                job.audio_queue.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def _fetch_loop(self):
        while not self._stopping:
            item = self._utterance_queue.get()
            if item is None:
                break
            if isinstance(item, tuple):
                text, chunk_id = item
            else:  # pragma: no cover
                text, chunk_id = item, None

            if self._consume_cancel_all() or self._is_cancelled(chunk_id):
                self._clear_cancelled(chunk_id)
                continue
            if not self._acquire_tts_slot():
                break

            job = _PhraseJob(text=text, chunk_id=chunk_id)
            self._put_play_item(job)
            worker = threading.Thread(target=self._fetch_phrase_job, args=(job,), daemon=True)
            with self._worker_threads_lock:
                self._worker_threads.add(worker)
            worker.start()

        self._put_play_item(None)

    def _acquire_tts_slot(self) -> bool:
        while not self._stopping:
            if self._tts_slots.acquire(timeout=0.1):
                return True
        return False

    def _fetch_phrase_job(self, job: _PhraseJob):
        current = threading.current_thread()
        try:
            if self._is_cancelled(job.chunk_id):
                job.cancelled.set()
                return

            self._emit_phrase_locked(job.chunk_id, True)
            job.send_ts = time.perf_counter()
            self._on_status("speaking")

            leftover = b""
            for chunk in self._iter_tts_audio(job):
                if self._stopping or self._is_cancelled(job.chunk_id):
                    job.cancelled.set()
                    return
                if not chunk:
                    continue
                buf = leftover + chunk
                usable_len = len(buf) - (len(buf) % 2)
                if usable_len == 0:
                    leftover = buf
                    continue
                leftover = buf[usable_len:]
                pcm = np.frombuffer(buf[:usable_len], dtype=np.int16)
                self._put_job_audio(job, pcm)
                job.recv_ts = time.perf_counter()
        except Exception as err:  # noqa: BLE001
            self._on_error(str(err))
        finally:
            self._put_job_audio(job, _PHRASE_END, force=True)
            self._tts_slots.release()
            with self._worker_threads_lock:
                self._worker_threads.discard(current)

    def _iter_tts_audio(self, job: _PhraseJob) -> Iterable[bytes]:
        with self._transport_lock:
            transport = self._transport

        if transport == "websocket":
            try:
                job.transport = "websocket"
                yield from self._iter_tts_websocket(job.text)
                return
            except Exception as err:  # noqa: BLE001
                log.warning(
                    "Fish Audio WebSocket TTS failed for backend=%s; "
                    "falling back to HTTP for this session: %s",
                    TTS_BACKEND,
                    err,
                )
                with self._transport_lock:
                    self._transport = "http"

        job.transport = "http"
        yield from self._iter_tts_http(job.text)

    def _tts_payload(self, text: str) -> dict:
        payload: dict = {
            "text": text,
            "format": "pcm",
            "sample_rate": OUTPUT_SAMPLE_RATE,
            "latency": _TTS_LATENCY,
            "chunk_length": _TTS_CHUNK_LENGTH,
            "min_chunk_length": _TTS_MIN_CHUNK_LENGTH,
            "condition_on_previous_chunks": True,
            "early_stop_threshold": 1,
            "normalize": True,
            "top_p": 0.7,
            "temperature": 0.7,
        }
        if self._reference_id:
            payload["reference_id"] = self._reference_id
        else:
            payload["references"] = [
                {
                    "audio": self._reference_audio_bytes,
                    "text": self._reference_transcript,
                }
            ]
        return payload

    def _iter_tts_http(self, text: str) -> Iterable[bytes]:
        with self._http_client_lock:
            if self._http_client.is_closed:
                self._http_client = self._make_http_client(self._api_key)
            client = self._http_client

        headers = {
            "Content-Type": "application/msgpack",
            "model": TTS_BACKEND,
        }
        with client.stream(
            "POST",
            "/v1/tts",
            headers=headers,
            content=ormsgpack.packb(self._tts_payload(text)),
        ) as response:
            if not response.is_success:
                body = response.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Fish Audio HTTP TTS {response.status_code}: {body}")
            for chunk in response.iter_bytes():
                yield chunk

    def _iter_tts_websocket(self, text: str) -> Iterable[bytes]:
        with self._http_client_lock:
            if self._http_client.is_closed:
                self._http_client = self._make_http_client(self._api_key)
            client = self._http_client

        with connect_ws(
            "/v1/tts/live",
            client=client,
            headers={"model": TTS_BACKEND},
        ) as ws:
            start_payload = self._tts_payload("")
            ws.send_bytes(ormsgpack.packb({"event": "start", "request": start_payload}))
            for fragment in self._text_fragments(text):
                ws.send_bytes(ormsgpack.packb({"event": "text", "text": fragment}))
            ws.send_bytes(ormsgpack.packb({"event": "flush"}))
            ws.send_bytes(ormsgpack.packb({"event": "stop"}))

            while True:
                try:
                    data = ormsgpack.unpackb(ws.receive_bytes())
                except WebSocketDisconnect as exc:
                    raise RuntimeError("Fish Audio WebSocket disconnected") from exc

                event = data.get("event") or data.get(b"event")
                if event == "audio":
                    audio = data.get("audio") or data.get(b"audio")
                    if audio:
                        yield audio
                elif event == "finish":
                    reason = data.get("reason") or data.get(b"reason")
                    if reason == "error":
                        raise RuntimeError("Fish Audio WebSocket TTS returned an error")
                    break

    @staticmethod
    def _text_fragments(text: str) -> Iterable[str]:
        words = text.split()
        if not words:
            return

        buf: list[str] = []
        length = 0
        target = max(20, min(_TTS_MIN_CHUNK_LENGTH, 80))
        for word in words:
            next_len = length + len(word) + (1 if buf else 0)
            if buf and next_len >= target:
                yield " ".join(buf) + " "
                buf = [word]
                length = len(word)
            else:
                buf.append(word)
                length = next_len
        if buf:
            yield " ".join(buf)

    def _play_loop(self):
        while True:
            item = self._play_queue.get()
            if item is None:
                break
            if isinstance(item, _FillerJob):
                self._write_pcm(item.pcm)
                self._maybe_inject_auto_filler()
                continue
            if isinstance(item, _PhraseJob):
                self._play_phrase_job(item)
        self._on_status("idle")

    def _play_phrase_job(self, job: _PhraseJob):
        if self._is_cancelled(job.chunk_id):
            job.cancelled.set()

        if not job.cancelled.is_set() and self._on_active_changed_cb:
            self._active_chunk_id = job.chunk_id
            try:
                self._on_active_changed_cb(job.chunk_id, job.text)
            except Exception:  # noqa: BLE001
                pass

        while True:
            item = job.audio_queue.get()
            if item is _PHRASE_END:
                break
            if self._stopping:
                return
            if self._is_cancelled(job.chunk_id):
                job.cancelled.set()
                continue
            self._write_pcm(item)

        if not job.cancelled.is_set() and not self._stopping:
            self._report_phrase_played(job)
            self._maybe_inject_auto_filler()

        if self._on_active_changed_cb and self._active_chunk_id == job.chunk_id:
            self._active_chunk_id = None
            try:
                self._on_active_changed_cb(None, None)
            except Exception:  # noqa: BLE001
                pass
        self._emit_phrase_locked(job.chunk_id, False)
        self._clear_cancelled(job.chunk_id)

        if self._play_queue.empty() and self._utterance_queue.empty():
            self._on_status("idle")

    def _write_pcm(self, pcm: np.ndarray):
        if self._stopping or not self._out_streams:
            return
        for out_stream in self._out_streams:
            try:
                out_stream.write(pcm)
            except Exception as err:  # noqa: BLE001
                self._on_error(str(err))

    def _report_phrase_played(self, job: _PhraseJob):
        if job.send_ts is None or job.recv_ts is None:
            return
        latency = max(job.recv_ts - job.send_ts, 0.0)
        with self._tts_latency_lock:
            self._tts_latencies.append(latency)
            p50, p95 = _latency_summary(self._tts_latencies)
            count = len(self._tts_latencies)

        log.info(
            "TTS latency backend=%s transport=%s chunk_id=%s current=%.0fms "
            "p50=%.0fms p95=%.0fms n=%d",
            TTS_BACKEND,
            job.transport,
            job.chunk_id,
            latency * 1000,
            p50 * 1000,
            p95 * 1000,
            count,
        )

        if self._on_phrase_played_cb:
            try:
                self._on_phrase_played_cb(job.chunk_id, job.text, latency)
            except Exception:  # noqa: BLE001
                pass

    def _maybe_inject_auto_filler(self):
        now = time.perf_counter()
        gap = (
            (now - self._last_phrase_end_ts)
            if self._last_phrase_end_ts is not None
            else 0.0
        )
        self._last_phrase_end_ts = now
        if not self._auto_filler_enabled or self._filler_play_cb is None:
            return
        if gap < _AUTO_FILLER_GAP_SECONDS:
            return
        path = None
        try:
            path = self._filler_play_cb(gap)
        except Exception as err:  # noqa: BLE001
            log.warning("Filler resolver raised: %s", err)
            return
        if path:
            log.info("Auto-filler gap=%.2fs -> %s", gap, path)
            self.play_filler(path)

    def _consume_cancel_all(self) -> bool:
        with self._cancel_lock:
            if not self._pending_cancel_all:
                return False
            self._pending_cancel_all = False
            self._cancelled_chunk_ids.clear()
            return True

    def _is_cancelled(self, chunk_id: Optional[str]) -> bool:
        if chunk_id is None:
            return False
        with self._cancel_lock:
            return chunk_id in self._cancelled_chunk_ids

    def _clear_cancelled(self, chunk_id: Optional[str]):
        if chunk_id is None:
            return
        with self._cancel_lock:
            self._cancelled_chunk_ids.discard(chunk_id)

    def _emit_phrase_locked(self, chunk_id: Optional[str], locked: bool):
        if not chunk_id or self._on_phrase_locked_cb is None:
            return
        try:
            self._on_phrase_locked_cb(chunk_id, locked)
        except Exception:  # noqa: BLE001
            pass
