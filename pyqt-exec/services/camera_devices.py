"""Enumerates available camera devices for the camera picker dropdown.

OpenCV/most OS camera backends don't expose friendly device names the
way audio APIs do, so we probe device indices directly (0, 1, 2, ...)
and report which ones actually open successfully. This mirrors what
most lightweight camera pickers do in the absence of a proper device
enumeration API.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2


@dataclass
class CameraDevice:
    index: int
    label: str

    def __str__(self):
        return self.label


def list_cameras(max_index: int = 8) -> list[CameraDevice]:
    """Probes device indices 0..max_index-1 and returns the ones that
    successfully open. Each probe opens and immediately releases the
    capture, so this is a bit slow (a few hundred ms per index) but
    only runs when the user opens the dropdown / refreshes it.
    """
    found: list[CameraDevice] = []
    for index in range(max_index):
        cap = cv2.VideoCapture(index)
        try:
            if cap.isOpened():
                ok, _ = cap.read()
                if ok:
                    found.append(CameraDevice(index=index, label=f"Camera {index}"))
        finally:
            cap.release()
    return found
