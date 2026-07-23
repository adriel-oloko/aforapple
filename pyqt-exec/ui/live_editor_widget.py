"""Live Editor widget -- port of components/LiveRealtimeEditor.tsx.

Webcam -> WebRTC -> fal.ai Lucy 2.5 realtime video model, with a prompt
box, optional reference-image upload (character swap / try-on), and an
expand/collapse toggle for the output view (Escape collapses it, mirroring
the original's keyboard handler).

WebRTC signaling protocol mirrors handleResult() in the original component:
the server sends `iceServers` first, we build an RTCPeerConnection, send
an SDP offer, then handle the `answer` and `icecandidate` messages that
come back.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import cv2
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, VideoStreamTrack
from aiortc.sdp import candidate_from_sdp
from av import VideoFrame
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QKeyEvent, QPixmap
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services import fal_client
from services.config import Config
from ui.status_badge import StatusBadge

DEFAULT_PROMPT = (
    "Substitute the character in the video with the person in the reference image"
)


class _WebcamVideoTrack(VideoStreamTrack):
    """Wraps an OpenCV VideoCapture as an aiortc video track -- this is
    the Python-side equivalent of getUserMedia's video track in the
    original JS, which fed the RTCPeerConnection directly.
    """

    def __init__(self, capture: cv2.VideoCapture):
        super().__init__()
        self._capture = capture

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        ok, frame_bgr = self._capture.read()
        if not ok:
            frame_bgr = self._blank_frame()
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame

    @staticmethod
    def _blank_frame():
        import numpy as np

        return np.zeros((480, 640, 3), dtype="uint8")


class LiveEditorWidget(QWidget):
    _status_changed = pyqtSignal(str)
    _error_occurred = pyqtSignal(str)
    _output_frame_ready = pyqtSignal(QImage)
    _input_frame_ready = pyqtSignal(QImage)

    def __init__(self, config: Config, async_loop: asyncio.AbstractEventLoop, parent=None):
        super().__init__(parent)
        self._config = config
        self._loop = async_loop

        self._capture: Optional[cv2.VideoCapture] = None
        self._capture_timer: Optional[QTimer] = None
        self._pc: Optional[RTCPeerConnection] = None
        self._connection: Optional[fal_client.RealtimeConnection] = None
        self._webcam_track: Optional[_WebcamVideoTrack] = None

        self._reference_image_url: Optional[str] = None
        self._expanded = False

        self._build_ui()
        self._wire_signals()

    # ── UI construction ─────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(20)

        # Header
        header_row = QHBoxLayout()
        title_col = QVBoxLayout()
        title = QLabel("Live Editor")
        title.setObjectName("sectionTitle")
        title_col.addWidget(title)
        subtitle = QLabel("Real-time video editing over WebRTC")
        subtitle.setObjectName("sectionSubtitle")
        title_col.addWidget(subtitle)
        header_row.addLayout(title_col)
        header_row.addStretch(1)
        self._status_badge = StatusBadge()
        header_row.addWidget(self._status_badge)
        layout.addLayout(header_row)

        # Video boxes
        video_row = QHBoxLayout()
        video_row.setSpacing(16)

        input_col = QVBoxLayout()
        input_label = QLabel("INPUT · YOUR CAMERA")
        input_label.setObjectName("fieldLabel")
        input_col.addWidget(input_label)
        self._input_view = QLabel("Camera feed will appear here")
        self._input_view.setObjectName("placeholder")
        self._input_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._input_view.setFixedHeight(280)
        self._input_view.setFrameShape(QFrame.Shape.Box)
        self._input_view.setStyleSheet("border: 1px solid rgba(255,255,255,0.10); background:#000;")
        input_col.addWidget(self._input_view)
        video_row.addLayout(input_col)

        output_col = QVBoxLayout()
        output_header = QHBoxLayout()
        output_label = QLabel("OUTPUT")
        output_label.setObjectName("fieldLabel")
        output_header.addWidget(output_label)
        output_header.addStretch(1)
        self._expand_btn = QPushButton("Expand view")
        self._expand_btn.setObjectName("secondary")
        self._expand_btn.clicked.connect(self._toggle_expand)
        output_header.addWidget(self._expand_btn)
        output_col.addLayout(output_header)

        self._output_view = QLabel("Edited feed will appear here")
        self._output_view.setObjectName("placeholder")
        self._output_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._output_view.setFixedHeight(280)
        self._output_view.setFrameShape(QFrame.Shape.Box)
        self._output_view.setStyleSheet("border: 1px solid rgba(255,255,255,0.10); background:#000;")
        output_col.addWidget(self._output_view)
        video_row.addLayout(output_col)

        layout.addLayout(video_row)

        # Prompt
        prompt_label = QLabel("EDIT INSTRUCTION")
        prompt_label.setObjectName("fieldLabel")
        layout.addWidget(prompt_label)

        prompt_row = QHBoxLayout()
        self._prompt_edit = QTextEdit()
        self._prompt_edit.setPlainText(DEFAULT_PROMPT)
        self._prompt_edit.setFixedHeight(56)
        prompt_row.addWidget(self._prompt_edit, stretch=1)

        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setObjectName("primary")
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._apply_prompt)
        prompt_row.addWidget(self._apply_btn)
        layout.addLayout(prompt_row)

        # Reference image
        ref_label = QLabel("REFERENCE IMAGE  (optional — character swap or try-on)")
        ref_label.setObjectName("fieldLabel")
        layout.addWidget(ref_label)

        ref_row = QHBoxLayout()
        self._ref_thumb = QLabel("None")
        self._ref_thumb.setFixedSize(64, 64)
        self._ref_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ref_thumb.setStyleSheet(
            "border: 1px dashed rgba(255,255,255,0.15); color: rgba(255,255,255,0.2); font-size: 10px;"
        )
        ref_row.addWidget(self._ref_thumb)

        ref_btn_col = QVBoxLayout()
        self._choose_ref_btn = QPushButton("Choose image")
        self._choose_ref_btn.setObjectName("secondary")
        self._choose_ref_btn.clicked.connect(self._pick_reference_image)
        ref_btn_col.addWidget(self._choose_ref_btn)
        self._clear_ref_btn = QPushButton("Remove")
        self._clear_ref_btn.setObjectName("ghost")
        self._clear_ref_btn.setVisible(False)
        self._clear_ref_btn.clicked.connect(self._clear_reference_image)
        ref_btn_col.addWidget(self._clear_ref_btn)
        ref_row.addLayout(ref_btn_col)
        ref_row.addStretch(1)
        layout.addLayout(ref_row)

        # Session control
        session_row = QHBoxLayout()
        self._start_btn = QPushButton("Start session")
        self._start_btn.setObjectName("primary")
        self._start_btn.clicked.connect(self._start_session)
        session_row.addWidget(self._start_btn)

        self._stop_btn = QPushButton("Stop session")
        self._stop_btn.setObjectName("secondary")
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self._stop_session)
        session_row.addWidget(self._stop_btn)

        session_row.addStretch(1)
        self._error_label = QLabel("")
        self._error_label.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 12px;")
        session_row.addWidget(self._error_label)
        layout.addLayout(session_row)

        layout.addStretch(1)

    def _wire_signals(self):
        self._status_changed.connect(self._on_status_changed)
        self._error_occurred.connect(self._on_error)
        self._output_frame_ready.connect(self._on_output_frame)
        self._input_frame_ready.connect(self._on_input_frame)

    # ── keyboard: Escape collapses expanded view ────────────────────────

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_Escape and self._expanded:
            self._toggle_expand()
        else:
            super().keyPressEvent(event)

    def _toggle_expand(self):
        self._expanded = not self._expanded
        self._expand_btn.setText("Collapse" if self._expanded else "Expand view")
        if self._expanded:
            self._output_view.setWindowFlags(Qt.WindowType.Window)
            self._output_view.showFullScreen()
        else:
            self._output_view.setWindowFlags(Qt.WindowType.Widget)
            self._output_view.show()

    # ── reference image ─────────────────────────────────────────────────

    def _pick_reference_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a reference image", "", "Images (*.jpg *.jpeg *.png *.webp)"
        )
        if not path:
            return
        pixmap = QPixmap(path).scaled(
            64, 64, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
        )
        self._ref_thumb.setPixmap(pixmap)
        self._ref_thumb.setText("")
        self._clear_ref_btn.setVisible(True)
        self._error_label.setText("")

        asyncio.run_coroutine_threadsafe(self._upload_reference(path), self._loop)

    async def _upload_reference(self, path: str):
        if not self._config.fal_key:
            self._error_occurred.emit("FAL_KEY not configured — add it to .env")
            return
        try:
            url = await fal_client.upload_reference_image(self._config.fal_key, path)
            self._reference_image_url = url
        except Exception as err:  # noqa: BLE001
            self._error_occurred.emit(f"Reference image upload failed: {err}")

    def _clear_reference_image(self):
        self._ref_thumb.clear()
        self._ref_thumb.setText("None")
        self._clear_ref_btn.setVisible(False)
        self._reference_image_url = None

    # ── session control ─────────────────────────────────────────────────

    def _start_session(self):
        self._error_label.setText("")
        missing = self._config.missing_for_video()
        if missing:
            self._on_error(f"Missing API key(s): {', '.join(missing)}. Add them to .env.")
            return

        self._status_changed.emit("requesting")
        self._capture = cv2.VideoCapture(0)
        if not self._capture.isOpened():
            self._on_error("Could not open webcam (device 0).")
            return

        self._capture_timer = QTimer(self)
        self._capture_timer.timeout.connect(self._pump_input_preview)
        self._capture_timer.start(33)  # ~30fps preview

        self._start_btn.setVisible(False)
        self._stop_btn.setVisible(True)

        asyncio.run_coroutine_threadsafe(self._connect_webrtc(), self._loop)

    def _stop_session(self):
        if self._capture_timer:
            self._capture_timer.stop()
            self._capture_timer = None
        if self._capture:
            self._capture.release()
            self._capture = None

        if self._pc or self._connection:
            asyncio.run_coroutine_threadsafe(self._teardown_webrtc(), self._loop)

        self._start_btn.setVisible(True)
        self._stop_btn.setVisible(False)
        self._apply_btn.setEnabled(False)
        self._expanded = False
        self._status_changed.emit("idle")

    async def _teardown_webrtc(self):
        if self._connection:
            await self._connection.close()
            self._connection = None
        if self._pc:
            await self._pc.close()
            self._pc = None

    async def _connect_webrtc(self):
        try:
            self._status_changed.emit("connecting")

            def on_message(msg: dict):
                asyncio.run_coroutine_threadsafe(self._handle_signal(msg), self._loop)

            def on_error(message: str):
                self._error_occurred.emit(message)

            self._connection = fal_client.RealtimeConnection(
                self._config.fal_key, on_message, on_error
            )
            await self._connection.connect()

            self._webcam_track = _WebcamVideoTrack(self._capture)

            await self._connection.send(
                {
                    "prompt": self._prompt_edit.toPlainText(),
                    "enable_prompt_expansion": True,
                    "reference_image_url": self._reference_image_url,
                }
            )
        except Exception as err:  # noqa: BLE001
            self._error_occurred.emit(str(err))

    async def _handle_signal(self, msg: dict):
        msg_type = (msg.get("type") or "").lower()
        try:
            if msg_type == "iceservers" and self._pc is None:
                ice_servers = msg.get("iceServers") or msg.get("iceservers") or msg.get("ice_servers") or []
                self._pc = RTCPeerConnection()

                if self._webcam_track:
                    self._pc.addTrack(self._webcam_track)

                @self._pc.on("track")
                def on_track(track):  # noqa: ANN001
                    if track.kind == "video":
                        asyncio.ensure_future(self._consume_output_track(track), loop=self._loop)

                @self._pc.on("icecandidate")
                async def on_icecandidate(candidate):  # noqa: ANN001
                    if candidate and self._connection:
                        await self._connection.send(
                            {
                                "type": "icecandidate",
                                "candidate": {
                                    "candidate": candidate.candidate,
                                    "sdpMid": candidate.sdpMid,
                                    "sdpMLineIndex": candidate.sdpMLineIndex,
                                },
                            }
                        )

                offer = await self._pc.createOffer()
                await self._pc.setLocalDescription(offer)
                if self._connection:
                    await self._connection.send({"type": "offer", "sdp": self._pc.localDescription.sdp})
                return

            if msg_type == "answer" and self._pc:
                await self._pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg.get("sdp"), type="answer")
                )
                self._status_changed.emit("live")
                self._apply_btn.setEnabled(True)
                return

            if msg_type == "icecandidate" and self._pc and msg.get("candidate"):
                cand_dict = msg["candidate"]
                candidate = candidate_from_sdp(cand_dict["candidate"])
                candidate.sdpMid = cand_dict.get("sdpMid")
                candidate.sdpMLineIndex = cand_dict.get("sdpMLineIndex")
                await self._pc.addIceCandidate(candidate)
                return

            if msg.get("error"):
                self._error_occurred.emit(str(msg["error"]))
        except Exception as err:  # noqa: BLE001
            self._error_occurred.emit(str(err))

    async def _consume_output_track(self, track):
        while True:
            try:
                frame = await track.recv()
            except Exception:
                break
            img = frame.to_ndarray(format="rgb24")
            h, w, _ = img.shape
            qimg = QImage(img.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
            self._output_frame_ready.emit(qimg)

    def _apply_prompt(self):
        if not self._connection:
            return
        asyncio.run_coroutine_threadsafe(
            self._connection.send(
                {
                    "prompt": self._prompt_edit.toPlainText(),
                    "enable_prompt_expansion": True,
                    "reference_image_url": self._reference_image_url,
                }
            ),
            self._loop,
        )

    # ── preview pump (local webcam mirror) ──────────────────────────────

    def _pump_input_preview(self):
        if not self._capture:
            return
        ok, frame_bgr = self._capture.read()
        if not ok:
            return
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        h, w, _ = frame_rgb.shape
        qimg = QImage(frame_rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
        self._input_frame_ready.emit(qimg)

    # ── main-thread slots ────────────────────────────────────────────────

    def _on_status_changed(self, status: str):
        self._status_badge.set_status(status)

    def _on_error(self, message: str):
        self._error_label.setText(message)
        self._status_badge.set_status("error")

    def _on_input_frame(self, image: QImage):
        pixmap = QPixmap.fromImage(image).scaled(
            self._input_view.width(),
            self._input_view.height(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._input_view.setPixmap(pixmap)

    def _on_output_frame(self, image: QImage):
        pixmap = QPixmap.fromImage(image).scaled(
            self._output_view.width(),
            self._output_view.height(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._output_view.setPixmap(pixmap)

    # ── cleanup ─────────────────────────────────────────────────────────

    def shutdown(self):
        self._stop_session()
