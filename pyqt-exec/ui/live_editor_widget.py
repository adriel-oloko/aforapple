"""Live Editor widget -- port of components/LiveRealtimeEditor.tsx.

Webcam -> WebRTC -> fal.ai Lucy 2.5 realtime video model, with a prompt
box, optional reference-image upload (character swap / try-on), a
camera picker, and an expand/collapse toggle for the output view
(Escape collapses it, mirroring the original's keyboard handler).

WebRTC signaling protocol mirrors handleResult() in the original component:
the server sends `iceServers` first, we build an RTCPeerConnection, send
an SDP offer, then handle the `answer` and `icecandidate` messages that
come back.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Optional

import cv2
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from aiortc.sdp import candidate_from_sdp, candidate_to_sdp
from av import VideoFrame
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
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

from services import camera_devices, fal_client
from services.config import Config
from services.session_log import get_logger
from ui.status_badge import StatusBadge
from ui.video_output_popup import VideoOutputPopup

DEFAULT_PROMPT = (
    "Substitute the character in the video with the person in the reference image"
)

# The video boxes used to be a fixed VIDEO_BOX_WIDTH x VIDEO_BOX_HEIGHT
# each -- two of those side by side (960px+) is wider than the window
# can shrink to, which forced the whole main window into a horizontal
# scrollbar. They're now responsive: each box has a minimum size only,
# grows/shrinks with the window, and the displayed frame is rescaled
# to fit whatever size the box currently is (see _ScalingVideoLabel
# below), so neither box can ever push the layout wider than the
# viewport.
VIDEO_BOX_MIN_WIDTH = 200
VIDEO_BOX_MIN_HEIGHT = 120

log = get_logger()


class _WebcamVideoTrack(VideoStreamTrack):
    """Wraps an OpenCV VideoCapture as an aiortc video track -- this is
    the Python-side equivalent of getUserMedia's video track in the
    original JS, which fed the RTCPeerConnection directly.

    The same VideoCapture is also polled by a QTimer on the Qt thread
    for the local preview mirror (see _pump_input_preview), while recv()
    here runs on the aiortc/asyncio loop's own thread. cv2.VideoCapture
    (particularly the Windows MSMF backend) is not thread-safe, so both
    readers -- and _stop_session()'s release() -- must serialize on the
    same lock or concurrent reads/release corrupt the capture's internal
    state (this is what produced the MSMF OnReadSample errors and the
    "QMutex: destroying locked mutex" crash on stop).
    """

    def __init__(self, capture: cv2.VideoCapture, lock: threading.Lock):
        super().__init__()
        self._capture = capture
        self._lock = lock
        self._stopped = False

    def stop_capture(self):
        """Marks this track's capture as invalid. Must be called (while
        holding `self._lock` from the caller's side, i.e. the same lock
        passed at construction) before the underlying VideoCapture is
        released, so an in-flight or subsequent recv() never touches a
        released capture handle.
        """
        self._stopped = True

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        with self._lock:
            if self._stopped or self._capture is None:
                ok, frame_bgr = False, None
            else:
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


class _ScalingVideoLabel(QLabel):
    """A video display label that fills whatever space the layout gives
    it (down to a small minimum) instead of forcing a fixed size, and
    keeps whatever frame is showing rescaled to fit its current size.
    This is what lets the two video boxes shrink together with the
    window instead of overflowing it horizontally.
    """

    def __init__(self, placeholder_text: str):
        super().__init__(placeholder_text)
        self._source_pixmap: Optional[QPixmap] = None

    def set_source_pixmap(self, pixmap: QPixmap):
        self._source_pixmap = pixmap
        self._rescale()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if self._source_pixmap is None or self._source_pixmap.isNull():
            return
        fitted = self._source_pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        super().setPixmap(fitted)


def _make_video_box(placeholder_text: str) -> _ScalingVideoLabel:
    """Builds a responsive video display label: it has a small minimum
    size (so it never disappears entirely) but otherwise expands and
    shrinks with the layout, keeping the displayed frame fitted to
    whatever size it currently is.
    """
    box = _ScalingVideoLabel(placeholder_text)
    box.setObjectName("placeholder")
    box.setAlignment(Qt.AlignmentFlag.AlignCenter)
    box.setMinimumSize(VIDEO_BOX_MIN_WIDTH, VIDEO_BOX_MIN_HEIGHT)
    box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    box.setFrameShape(QFrame.Shape.Box)
    box.setScaledContents(False)
    box.setStyleSheet("border: 1px solid rgba(255,255,255,0.10); background:#000;")
    return box


class LiveEditorWidget(QWidget):
    _status_changed = pyqtSignal(str)
    _error_occurred = pyqtSignal(str)
    _output_frame_ready = pyqtSignal(QImage)
    _input_frame_ready = pyqtSignal(QImage)

    # Fired when the output view expands/collapses so MainWindow can
    # show/hide the Advanced Audio popup in step -- expanding takes
    # over the app's own viewport (not the whole OS screen), which is
    # also where the inline Advanced Audio panel normally lives.
    expanded_changed = pyqtSignal(bool)

    def __init__(self, config: Config, async_loop: asyncio.AbstractEventLoop, parent=None):
        super().__init__(parent)
        self._config = config
        self._loop = async_loop

        self._capture: Optional[cv2.VideoCapture] = None
        self._capture_timer: Optional[QTimer] = None
        self._capture_lock = threading.Lock()
        self._pc: Optional[RTCPeerConnection] = None
        self._connection: Optional[fal_client.RealtimeConnection] = None
        self._webcam_track: Optional[_WebcamVideoTrack] = None
        self._ice_trickle_task: Optional[asyncio.Task] = None
        self._sent_candidates: set[str] = set()

        self._reference_image_url: Optional[str] = None
        self._expanded = False
        self._last_output_pixmap: Optional[QPixmap] = None
        self._output_popup: Optional[VideoOutputPopup] = None

        self._build_ui()
        self._wire_signals()
        self._refresh_camera_list()

    # -- UI construction ------------------------------------------------

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

        # Camera picker
        camera_row = QHBoxLayout()
        camera_label = QLabel("CAMERA")
        camera_label.setObjectName("fieldLabel")
        camera_row.addWidget(camera_label)
        self._camera_combo = QComboBox()
        # Same fix as the audio device combos in AdvancedAudioWidget:
        # don't let a long camera name (or the old hard-coded 220px
        # floor) become a minimum width the window can't shrink below.
        self._camera_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._camera_combo.setMinimumContentsLength(10)
        camera_row.addWidget(self._camera_combo, stretch=1)
        refresh_camera_btn = QPushButton("Refresh")
        refresh_camera_btn.setObjectName("ghost")
        refresh_camera_btn.clicked.connect(self._refresh_camera_list)
        camera_row.addWidget(refresh_camera_btn)
        camera_row.addStretch(1)
        layout.addLayout(camera_row)

        # Video boxes -- both fixed-size so neither can push the other
        # off-screen once real frames start rendering.
        video_row = QHBoxLayout()
        video_row.setSpacing(16)

        input_col = QVBoxLayout()
        input_label = QLabel("INPUT - YOUR CAMERA")
        input_label.setObjectName("fieldLabel")
        input_col.addWidget(input_label)
        self._input_view = _make_video_box("Camera feed will appear here")
        input_col.addWidget(self._input_view)
        video_row.addLayout(input_col, 1)

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

        self._output_view = _make_video_box("Edited feed will appear here")
        output_col.addWidget(self._output_view)
        video_row.addLayout(output_col, 1)

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
        ref_label = QLabel("REFERENCE IMAGE  (optional -- character swap or try-on)")
        ref_label.setObjectName("fieldLabel")
        # Without word wrap, a QLabel's minimum width is its full
        # unwrapped text width, which would stop the window from
        # shrinking narrower than this (long) sentence and force a
        # horizontal scrollbar. Wrapping lets it shrink with everything
        # else.
        ref_label.setWordWrap(True)
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
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 12px;")
        session_row.addWidget(self._error_label, stretch=1)
        layout.addLayout(session_row)

        layout.addStretch(1)

    def _wire_signals(self):
        self._status_changed.connect(self._on_status_changed)
        self._error_occurred.connect(self._on_error)
        self._output_frame_ready.connect(self._on_output_frame)
        self._input_frame_ready.connect(self._on_input_frame)

    # -- camera picker ----------------------------------------------------

    def _refresh_camera_list(self):
        was_running = self._capture is not None
        if was_running:
            log.warning(
                "Camera list refreshed while a session is active; "
                "restart the session to apply a new selection."
            )

        self._camera_combo.clear()
        cameras = camera_devices.list_cameras()
        if not cameras:
            self._camera_combo.addItem("No camera found", userData=0)
            log.warning("No cameras detected during probe.")
        else:
            for cam in cameras:
                self._camera_combo.addItem(cam.label, userData=cam.index)
            log.info("Found %d camera(s): %s", len(cameras), [c.label for c in cameras])

    # -- expand / collapse -------------------------------------------------

    def _toggle_expand(self):
        if self._expanded:
            self._collapse_expanded_view()
        else:
            self._expand_view()

    def collapse_expanded_view(self):
        """Public entry point for external callers (e.g. when a
        session is stopped) to force a collapse."""
        self._collapse_expanded_view()

    def _expand_view(self):
        self._expanded = True
        self._expand_btn.setText("Collapse")

        # "Expand view" opens the output feed in its own top-level
        # window -- see ui/video_output_popup.py -- the same way the
        # Advanced Audio controls get their own popup window. This
        # never opens (or otherwise touches) that Advanced Audio
        # popup; the two are independent.
        self._output_popup = VideoOutputPopup(self)
        self._output_popup.close_requested.connect(self._collapse_expanded_view)
        self._output_popup.set_status(self._status_badge._status)
        if self._last_output_pixmap:
            self._output_popup.set_source_pixmap(self._last_output_pixmap)
        self._output_popup.show()
        log.info("Output view expanded into its own window")
        self.expanded_changed.emit(True)

    def _collapse_expanded_view(self):
        if not self._expanded:
            return
        self._expanded = False
        self._expand_btn.setText("Expand view")
        popup, self._output_popup = self._output_popup, None
        if popup is not None:
            popup.close_requested.disconnect(self._collapse_expanded_view)
            popup.close()
        log.info("Output view popup closed")
        self.expanded_changed.emit(False)

    # -- reference image ---------------------------------------------------

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
        log.info("Reference image selected: %s", path)

        asyncio.run_coroutine_threadsafe(self._upload_reference(path), self._loop)

    async def _upload_reference(self, path: str):
        if not self._config.fal_key:
            self._error_occurred.emit("FAL_KEY not configured -- add it to .env")
            return
        try:
            log.info("Uploading reference image to fal storage...")
            url = await fal_client.upload_reference_image(self._config.fal_key, path)
            self._reference_image_url = url
            log.info("Reference image uploaded: %s", url)
        except Exception as err:  # noqa: BLE001
            log.error("Reference image upload failed: %s", err)
            self._error_occurred.emit(f"Reference image upload failed: {err}")

    def _clear_reference_image(self):
        self._ref_thumb.clear()
        self._ref_thumb.setText("None")
        self._clear_ref_btn.setVisible(False)
        self._reference_image_url = None
        log.info("Reference image cleared")

    # -- session control ----------------------------------------------------

    def _start_session(self):
        self._error_label.setText("")
        missing = self._config.missing_for_video()
        if missing:
            self._on_error(f"Missing API key(s): {', '.join(missing)}. Add them to .env.")
            return

        camera_index = self._camera_combo.currentData()
        if camera_index is None:
            self._on_error("No camera selected.")
            return

        log.info("Starting session (camera index %s)...", camera_index)
        self._status_changed.emit("requesting")
        self._capture = cv2.VideoCapture(camera_index)
        if not self._capture.isOpened():
            log.error("Could not open camera index %s", camera_index)
            self._on_error(f"Could not open camera (index {camera_index}).")
            return

        self._capture_timer = QTimer(self)
        self._capture_timer.timeout.connect(self._pump_input_preview)
        self._capture_timer.start(33)  # ~30fps preview

        self._start_btn.setVisible(False)
        self._stop_btn.setVisible(True)
        self._camera_combo.setEnabled(False)

        asyncio.run_coroutine_threadsafe(self._connect_webrtc(), self._loop)

    def _stop_session(self):
        log.info("Stopping session...")
        if self._capture_timer:
            self._capture_timer.stop()
            self._capture_timer = None
        if self._capture:
            with self._capture_lock:
                if self._webcam_track is not None:
                    self._webcam_track.stop_capture()
                capture, self._capture = self._capture, None
                capture.release()

        if self._pc or self._connection:
            asyncio.run_coroutine_threadsafe(self._teardown_webrtc(), self._loop)

        self._start_btn.setVisible(True)
        self._stop_btn.setVisible(False)
        self._camera_combo.setEnabled(True)
        self._apply_btn.setEnabled(False)
        if self._expanded:
            self._collapse_expanded_view()
        self._status_changed.emit("idle")
        log.info("Session stopped")

    async def _teardown_webrtc(self):
        if self._ice_trickle_task:
            self._ice_trickle_task.cancel()
            self._ice_trickle_task = None
        self._sent_candidates.clear()
        if self._connection:
            await self._connection.close()
            self._connection = None
        if self._pc:
            await self._pc.close()
            self._pc = None

    async def _connect_webrtc(self):
        try:
            self._status_changed.emit("connecting")
            log.info("Requesting realtime token from fal.ai...")

            def on_message(msg: dict):
                asyncio.run_coroutine_threadsafe(self._handle_signal(msg), self._loop)

            def on_error(message: str):
                log.error("fal realtime connection error: %s", message)
                self._error_occurred.emit(message)

            self._connection = fal_client.RealtimeConnection(
                self._config.fal_key, on_message, on_error
            )
            await self._connection.connect()
            log.info("Connected to fal realtime signaling channel")

            self._webcam_track = _WebcamVideoTrack(self._capture, self._capture_lock)

            await self._connection.send(
                {
                    "prompt": self._prompt_edit.toPlainText(),
                    "enable_prompt_expansion": True,
                    "reference_image_url": self._reference_image_url,
                }
            )
            log.info("Sent initial prompt to Lucy 2.5")
        except Exception as err:  # noqa: BLE001
            log.error("WebRTC connection failed: %s", err)
            self._error_occurred.emit(str(err))

    # aiortc has no per-candidate "icecandidate" event the way a browser
    # does (see _trickle_ice_candidates below), so we poll for newly
    # gathered candidates instead. This is just the poll interval, not
    # a hard timeout -- fal expects candidates trickled as they appear,
    # not withheld until gathering finishes.
    _ICE_POLL_INTERVAL_SECONDS = 0.1

    async def _trickle_ice_candidates(self):
        """Poll aiortc's ICE gatherer for newly discovered local
        candidates and send each one to fal as its own `icecandidate`
        message, the way a browser's `pc.onicecandidate` handler would.

        aiortc's RTCPeerConnection has no public per-candidate callback
        -- `RTCIceGatherer.gather()` is a single coroutine that only
        returns once gathering is fully complete, and there is no event
        emitted per candidate. `getLocalCandidates()` is available and
        does grow incrementally while gathering is still in progress
        though, so we poll it instead.

        This matters because fal's realtime signaling server (per
        Decart's own reference implementation for this WebRTC protocol
        family) expects the initial `offer` sent immediately, with
        candidates trickled in afterwards -- not a single offer that
        waits for ICE gathering to finish before it's sent. Blocking on
        full gathering before sending anything meant fal's server was
        never sent an offer within the window it waits for one, and it
        gave up with a signaling TIMEOUT.
        """
        try:
            while self._pc is not None and self._pc.iceGatheringState != "complete":
                await self._send_new_local_candidates()
                await asyncio.sleep(self._ICE_POLL_INTERVAL_SECONDS)
            # Catch any candidates that landed in the final gap between
            # the last poll and gathering completing.
            await self._send_new_local_candidates()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            log.error("Error trickling ICE candidates: %s", err)

    async def _send_new_local_candidates(self):
        if self._pc is None or self._connection is None:
            return
        for transceiver in self._pc.getTransceivers():
            sender = transceiver.sender
            dtls_transport = getattr(sender, "transport", None)
            ice_transport = getattr(dtls_transport, "transport", None)
            ice_gatherer = getattr(ice_transport, "iceGatherer", None)
            if ice_gatherer is None:
                continue
            for candidate in ice_gatherer.getLocalCandidates():
                key = f"{candidate.foundation}:{candidate.component}:{candidate.port}:{candidate.protocol}"
                if key in self._sent_candidates:
                    continue
                self._sent_candidates.add(key)
                await self._connection.send(
                    {
                        "type": "icecandidate",
                        "candidate": {
                            "candidate": candidate_to_sdp(candidate),
                            "sdpMid": transceiver.mid,
                            "sdpMLineIndex": 0,
                        },
                    }
                )

    async def _handle_signal(self, msg: dict):
        msg_type = (msg.get("type") or "").lower()
        try:
            if msg_type == "iceservers" and self._pc is None:
                ice_servers = msg.get("iceServers") or msg.get("iceservers") or msg.get("ice_servers") or []
                log.info("Received ICE servers, creating RTCPeerConnection")

                rtc_ice_servers = []
                for entry in ice_servers:
                    urls = entry.get("urls") or entry.get("url")
                    if not urls:
                        continue
                    rtc_ice_servers.append(
                        RTCIceServer(
                            urls=urls,
                            username=entry.get("username"),
                            credential=entry.get("credential"),
                        )
                    )

                self._pc = RTCPeerConnection(
                    configuration=RTCConfiguration(iceServers=rtc_ice_servers)
                )

                if self._webcam_track:
                    self._pc.addTrack(self._webcam_track)

                @self._pc.on("track")
                def on_track(track):  # noqa: ANN001
                    if track.kind == "video":
                        log.info("Receiving edited output track")
                        asyncio.ensure_future(self._consume_output_track(track), loop=self._loop)

                offer = await self._pc.createOffer()
                await self._pc.setLocalDescription(offer)

                # Send the offer immediately, then trickle ICE
                # candidates in afterwards as they're discovered. fal's
                # signaling server (mirroring Decart's own browser
                # reference client for this protocol family) expects
                # the offer right away and candidates sent one at a
                # time via separate `icecandidate` messages -- it does
                # not wait for a single offer with gathering already
                # complete. Waiting for full ICE gathering before
                # sending anything meant fal never received an offer
                # inside the window it waits for one, and gave up with
                # a signaling TIMEOUT.
                if self._connection:
                    await self._connection.send({"type": "offer", "sdp": self._pc.localDescription.sdp})
                    log.info("Sent SDP offer")

                self._ice_trickle_task = asyncio.ensure_future(
                    self._trickle_ice_candidates(), loop=self._loop
                )
                return

            if msg_type == "answer" and self._pc:
                await self._pc.setRemoteDescription(
                    RTCSessionDescription(sdp=msg.get("sdp"), type="answer")
                )
                log.info("Received SDP answer -- session is live")
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
                log.error("Signaling error from fal: %s", msg["error"])
                self._error_occurred.emit(str(msg["error"]))
        except Exception as err:  # noqa: BLE001
            log.error("Error handling signal: %s", err)
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
        log.info("Applying updated prompt")
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

    # -- preview pump (local webcam mirror) ---------------------------------

    def _pump_input_preview(self):
        if not self._capture:
            return
        with self._capture_lock:
            if not self._capture:
                return
            ok, frame_bgr = self._capture.read()
        if not ok:
            return
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        h, w, _ = frame_rgb.shape
        qimg = QImage(frame_rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
        self._input_frame_ready.emit(qimg)

    # -- main-thread slots ----------------------------------------------------

    def _on_status_changed(self, status: str):
        self._status_badge.set_status(status)
        if self._output_popup is not None:
            self._output_popup.set_status(status)

    def _on_error(self, message: str):
        log.error(message)
        self._error_label.setText(message)
        self._status_badge.set_status("error")

    def _on_input_frame(self, image: QImage):
        # _ScalingVideoLabel fits this to its own current size (and
        # re-fits automatically whenever the window/box is resized),
        # so the pixmap is handed over at full resolution here.
        self._input_view.set_source_pixmap(QPixmap.fromImage(image))

    def _on_output_frame(self, image: QImage):
        pixmap = QPixmap.fromImage(image)
        self._last_output_pixmap = pixmap
        self._output_view.set_source_pixmap(pixmap)

        if self._output_popup is not None:
            self._output_popup.set_source_pixmap(pixmap)

    # -- cleanup ----------------------------------------------------------

    def shutdown(self):
        self._stop_session()