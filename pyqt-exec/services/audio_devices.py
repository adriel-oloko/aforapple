"""Enumerates audio input/output devices (headphones, virtual cables,
built-in mic, etc.) for the Advanced Audio dropdowns.
"""

from dataclasses import dataclass

import sounddevice as sd


@dataclass
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float

    def __str__(self):
        return self.name


def list_input_devices() -> list[AudioDevice]:
    return [d for d in _all_devices() if d.max_input_channels > 0]


def list_output_devices() -> list[AudioDevice]:
    return [d for d in _all_devices() if d.max_output_channels > 0]


def _all_devices() -> list[AudioDevice]:
    devices = []
    try:
        raw_devices = sd.query_devices()
    except Exception:
        return devices

    for i, d in enumerate(raw_devices):
        devices.append(
            AudioDevice(
                index=i,
                name=d.get("name", f"Device {i}"),
                max_input_channels=d.get("max_input_channels", 0),
                max_output_channels=d.get("max_output_channels", 0),
                default_samplerate=d.get("default_samplerate", 44100.0),
            )
        )
    return devices


def default_input_device() -> AudioDevice | None:
    try:
        idx = sd.default.device[0]
    except Exception:
        return None
    for d in list_input_devices():
        if d.index == idx:
            return d
    return None


def default_output_device() -> AudioDevice | None:
    try:
        idx = sd.default.device[1]
    except Exception:
        return None
    for d in list_output_devices():
        if d.index == idx:
            return d
    return None
