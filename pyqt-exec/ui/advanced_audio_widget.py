"""Advanced Audio (Voice Clone) section.

Collapsible panel matching the rest of the app's visual language. Lets
the user:
  1. Pick a reference audio clip of the voice to clone (+ its transcript,
     which Fish Audio's on-the-fly clone mode requires)
  2. Pick a mic (input) device
  3. Pick a playback (output) device
  4. Start/stop the live pipeline: mic -> AssemblyAI (STT) -> Fish Audio
     s2.1-pro (cloned-voice TTS) -> chosen output device

All AI/audio work happens on background threads (see services/); this
widget only ever touches its own widgets from Qt signal handlers running
on the main thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QComboBox,
    QWidget,
)

from services import audio_devices
from services.assemblyai_stream import AssemblyAIStreamer
from services.config import Config
from services.fish_audio_client import FishVoiceCloneSpeaker
from services.mic_capture import MicCapture
from ui.status_badge import StatusBadge


class AdvancedAudioWidget(QWidget):
    # Emitted from background threads; connected to slots that run on
    # the main thread so widget updates stay Qt-safe.
    _final_text_ready = pyqtSignal(str)
    _partial_text_ready = pyqtSignal(str)
    _stt_status_changed = pyqtSignal(str)
    _tts_status_changed = pyqtSignal(str)
    _error_occurred = pyqtSignal(str)

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self._config = config
        self._reference_path: Optional[str] = None
        self._streamer: Optional[AssemblyAIStreamer] = None
        self._speaker: Optional[FishVoiceCloneSpeaker] = None
        self._mic: Optional[MicCapture] = None
        self._running = False

        self._build_ui()
        self._wire_signals()
        self._refresh_device_lists()

    # ── UI construction ─────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Collapsible header (collapsed by default, mirrors the reference
        # image section's understated disclosure style)
        self._toggle_btn = QToolButton()
        self._toggle_btn.setObjectName("collapseHeader")
        self._toggle_btn.setText("▸  ADVANCED · AUDIO (VOICE CLONE)")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setChecked(False)
        self._toggle_btn.clicked.connect(self._on_toggle)
        outer.addWidget(self._toggle_btn)

        self._body = QFrame()
        self._body.setObjectName("sectionFrame")
        self._body.setVisible(False)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(16, 16, 16, 16)
        body_layout.setSpacing(14)

        body_layout.addWidget(self._build_reference_section())
        body_layout.addWidget(self._build_device_section())
        body_layout.addWidget(self._build_pipeline_section())

        outer.addWidget(self._body)

    def _build_reference_section(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        label = QLabel("VOICE TO CLONE")
        label.setObjectName("fieldLabel")
        layout.addWidget(label)

        row = QHBoxLayout()
        self._choose_file_btn = QPushButton("Choose audio file")
        self._choose_file_btn.setObjectName("secondary")
        self._choose_file_btn.clicked.connect(self._pick_reference_file)
        row.addWidget(self._choose_file_btn)

        self._reference_file_label = QLabel("No file selected")
        self._reference_file_label.setObjectName("placeholder")
        row.addWidget(self._reference_file_label, stretch=1)
        layout.addLayout(row)

        transcript_label = QLabel(
            "REFERENCE TRANSCRIPT  (what's spoken in the clip above — required for cloning)"
        )
        transcript_label.setObjectName("fieldLabel")
        layout.addWidget(transcript_label)

        self._reference_transcript_edit = QLineEdit()
        self._reference_transcript_edit.setPlaceholderText(
            "Type exactly what is said in the reference clip…"
        )
        layout.addWidget(self._reference_transcript_edit)

        return w

    def _build_device_section(self) -> QWidget:
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        input_col = QVBoxLayout()
        input_label = QLabel("AUDIO INPUT (MIC)")
        input_label.setObjectName("fieldLabel")
        input_col.addWidget(input_label)
        self._input_combo = QComboBox()
        input_col.addWidget(self._input_combo)
        layout.addLayout(input_col)

        output_col = QVBoxLayout()
        output_label = QLabel("AUDIO OUTPUT (PLAYBACK)")
        output_label.setObjectName("fieldLabel")
        output_col.addWidget(output_label)
        self._output_combo = QComboBox()
        output_col.addWidget(self._output_combo)
        layout.addLayout(output_col)

        refresh_col = QVBoxLayout()
        refresh_col.addWidget(QLabel(""))  # spacer to align with combo row
        refresh_btn = QPushButton("Refresh devices")
        refresh_btn.setObjectName("ghost")
        refresh_btn.clicked.connect(self._refresh_device_lists)
        refresh_col.addWidget(refresh_btn)
        layout.addLayout(refresh_col)

        return w

    def _build_pipeline_section(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        controls_row = QHBoxLayout()
        self._start_btn = QPushButton("Start voice clone")
        self._start_btn.setObjectName("primary")
        self._start_btn.clicked.connect(self._start_pipeline)
        controls_row.addWidget(self._start_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setObjectName("secondary")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop_pipeline)
        controls_row.addWidget(self._stop_btn)

        controls_row.addStretch(1)

        self._stt_badge = StatusBadge()
        controls_row.addWidget(self._stt_badge)
        self._tts_badge = StatusBadge()
        controls_row.addWidget(self._tts_badge)

        layout.addLayout(controls_row)

        transcript_label = QLabel("LIVE TRANSCRIPT")
        transcript_label.setObjectName("fieldLabel")
        layout.addWidget(transcript_label)

        self._transcript_log = QPlainTextEdit()
        self._transcript_log.setReadOnly(True)
        self._transcript_log.setFixedHeight(120)
        self._transcript_log.setPlaceholderText(
            "Finalized speech will appear here as it's sent to the cloned voice…"
        )
        layout.addWidget(self._transcript_log)

        self._error_label = QLabel("")
        self._error_label.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 12px;")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)

        return w

    # ── signal wiring ───────────────────────────────────────────────────

    def _wire_signals(self):
        self._final_text_ready.connect(self._on_final_text_main_thread)
        self._partial_text_ready.connect(self._on_partial_text_main_thread)
        self._stt_status_changed.connect(self._stt_badge.set_status)
        self._tts_status_changed.connect(self._tts_badge.set_status)
        self._error_occurred.connect(self._on_error_main_thread)

    # ── collapse toggle ─────────────────────────────────────────────────

    def _on_toggle(self):
        expanded = self._toggle_btn.isChecked()
        self._body.setVisible(expanded)
        arrow = "▾" if expanded else "▸"
        self._toggle_btn.setText(f"{arrow}  ADVANCED · AUDIO (VOICE CLONE)")

    # ── reference file picker ───────────────────────────────────────────

    def _pick_reference_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a reference clip of the voice to clone",
            "",
            "Audio files (*.wav *.mp3 *.m4a *.flac);;All files (*)",
        )
        if not path:
            return
        self._reference_path = path
        self._reference_file_label.setText(Path(path).name)
        self._reference_file_label.setObjectName("")
        self._reference_file_label.setStyleSheet("color: rgba(255,255,255,0.7);")

    # ── device lists ────────────────────────────────────────────────────

    def _refresh_device_lists(self):
        self._input_combo.clear()
        for d in audio_devices.list_input_devices():
            self._input_combo.addItem(d.name, userData=d.index)

        self._output_combo.clear()
        for d in audio_devices.list_output_devices():
            self._output_combo.addItem(d.name, userData=d.index)

        default_in = audio_devices.default_input_device()
        if default_in:
            idx = self._input_combo.findData(default_in.index)
            if idx >= 0:
                self._input_combo.setCurrentIndex(idx)

        default_out = audio_devices.default_output_device()
        if default_out:
            idx = self._output_combo.findData(default_out.index)
            if idx >= 0:
                self._output_combo.setCurrentIndex(idx)

    # ── pipeline control ────────────────────────────────────────────────

    def _start_pipeline(self):
        self._error_label.setText("")

        missing = self._config.missing_for_voice_clone()
        if missing:
            self._show_error(f"Missing API key(s): {', '.join(missing)}. Add them to .env.")
            return
        if not self._reference_path:
            self._show_error("Choose a reference audio file of the voice to clone first.")
            return
        if not self._reference_transcript_edit.text().strip():
            self._show_error("Enter the transcript of the reference clip (required for cloning).")
            return

        reference_bytes = Path(self._reference_path).read_bytes()
        reference_transcript = self._reference_transcript_edit.text().strip()
        output_device_index = self._output_combo.currentData()
        input_device_index = self._input_combo.currentData()

        self._speaker = FishVoiceCloneSpeaker(
            api_key=self._config.fish_key,
            reference_audio_bytes=reference_bytes,
            reference_transcript=reference_transcript,
            output_device_index=output_device_index,
            on_status=lambda s: self._tts_status_changed.emit(s),
            on_error=lambda e: self._error_occurred.emit(e),
        )
        self._speaker.start()

        self._streamer = AssemblyAIStreamer(
            api_key=self._config.assemblyai_key,
            on_partial_text=lambda t: self._partial_text_ready.emit(t),
            on_final_text=lambda t: self._final_text_ready.emit(t),
            on_status=lambda s: self._stt_status_changed.emit(s),
            on_error=lambda e: self._error_occurred.emit(e),
        )
        self._streamer.start()

        self._mic = MicCapture(
            input_device_index=input_device_index,
            on_chunk=self._streamer.push_audio,
        )
        self._mic.start()

        self._running = True
        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)

    def _stop_pipeline(self):
        if self._mic:
            self._mic.stop()
            self._mic = None
        if self._streamer:
            self._streamer.stop()
            self._streamer = None
        if self._speaker:
            self._speaker.stop()
            self._speaker = None

        self._running = False
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._stt_badge.set_status("idle")
        self._tts_badge.set_status("idle")

    # ── main-thread slots (safe to touch widgets here) ─────────────────

    def _on_final_text_main_thread(self, text: str):
        self._transcript_log.appendPlainText(f"› {text}")
        if self._speaker:
            self._speaker.enqueue_text(text)

    def _on_partial_text_main_thread(self, text: str):
        # Lightweight feedback only -- not sent to Fish Audio.
        self._transcript_log.setPlaceholderText(text)

    def _on_error_main_thread(self, message: str):
        self._show_error(message)

    def _show_error(self, message: str):
        self._error_label.setText(message)

    # ── cleanup ─────────────────────────────────────────────────────────

    def shutdown(self):
        if self._running:
            self._stop_pipeline()
