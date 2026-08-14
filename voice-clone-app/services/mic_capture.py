"""Opens the user-selected input device and streams 16kHz mono 16-bit
PCM chunks to a callback (AssemblyAIStreamer.push_audio) in real time.

sounddevice runs its callback on its own internal audio thread, so
push_audio() must be thread-safe on the receiving end (it is -- it just
puts bytes on a queue.Queue).
"""

from __future__ import annotations

from typing import Callable, Optional

import sounddevice as sd

SAMPLE_RATE = 16000
BLOCK_SIZE = 800  # 50ms at 16kHz


class MicCapture:
    def __init__(self, input_device_index: Optional[int], on_chunk: Callable[[bytes], None]):
        self._input_device_index = input_device_index
        self._on_chunk = on_chunk
        self._stream: Optional[sd.InputStream] = None

    def start(self):
        if self._stream is not None:
            return
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=BLOCK_SIZE,
            device=self._input_device_index,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        self._on_chunk(bytes(indata))
