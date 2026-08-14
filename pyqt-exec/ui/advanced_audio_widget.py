"""Advanced Audio (Voice Clone) section.

Collapsible panel matching the rest of the app's visual language. Lets
the user:
  1. Pick a reference audio clip of the voice to clone (+ its transcript,
     which Fish Audio's on-the-fly clone mode requires)
  2. Pick a mic (input) device
  3. Pick a playback (output) device, plus an optional second one to
     mirror playback to simultaneously (e.g. your speakers *and* a
     virtual cable another app can pick up as its mic input)
  4. Start/stop the live pipeline: mic -> AssemblyAI (STT) -> Fish Audio
     s2.1-pro (cloned-voice TTS) -> chosen output device

The reference audio is pre-uploaded to Fish Audio as a persistent voice
model (POST /model, train_mode=fast) the first time a profile is used.
Subsequent sessions reuse the model _id, eliminating the per-phrase
speaker-embedding computation that cost ~2-4s of latency.

All AI/audio work happens on background threads (see services/); this
widget only ever touches its own widgets from Qt signal handlers running
on the main thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QComboBox,
    QWidget,
)

from services import audio_devices
from services import voice_profiles
from services.assemblyai_stream import AssemblyAIStreamer
from services.config import Config
from services.fish_audio_client import FishVoiceCloneSpeaker
from services.mic_capture import MicCapture
from services.session_log import get_logger
from ui.status_badge import StatusBadge
from ui.text_block_list import TextBlockList

log = get_logger()


def _make_shrinkable_combo() -> QComboBox:
    """A QComboBox whose width tracks its layout column instead of the
    longest item in its dropdown list.

    By default QComboBox sizes itself (and sets its minimum width) to
    fit the longest item ever added -- device names like "Speakers
    (Realtek(R) Audio) - Realtek High Definition Audio" can easily be
    250px+ wide. With four of these side by side that alone was enough
    to overflow the window, even though every *other* widget in the
    app was already responsive. AdjustToMinimumContentsLengthWithIcon
    + a small minimumContentsLength caps the box's own minimum size;
    Qt still elides the displayed text with "..." if the box ends up
    narrower than the selected item's full name.
    """
    combo = QComboBox()
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(10)
    return combo


class AdvancedAudioWidget(QWidget):
    # Emitted from background threads; connected to slots that run on
    # the main thread so widget updates stay Qt-safe.
    _final_text_ready = pyqtSignal(str)
    _partial_text_ready = pyqtSignal(str)
    _stt_status_changed = pyqtSignal(str)
    _tts_status_changed = pyqtSignal(str)
    _error_occurred = pyqtSignal(str)

    # Extra signals for the new text-block / latency / filler features.
    _block_added = pyqtSignal(str, str)  # chunk_id, text
    _block_active_changed = pyqtSignal(object, object)  # chunk_id|None, text|None
    _block_done = pyqtSignal(str)  # chunk_id
    _block_locked_changed = pyqtSignal(str, bool)  # chunk_id, locked
    _stt_latency_ready = pyqtSignal(str, float)  # chunk_id, seconds
    _tts_latency_ready = pyqtSignal(str, float)  # chunk_id, seconds

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self._config = config
        self._reference_path: Optional[str] = None
        self._current_profile: Optional[voice_profiles.VoiceProfile] = None
        self._streamer: Optional[AssemblyAIStreamer] = None
        self._speaker: Optional[FishVoiceCloneSpeaker] = None
        self._mic: Optional[MicCapture] = None
        self._running = False

        # Maps chunk_id -> text for pending blocks, so the STT
        # on_latency callback (which reports text, not chunk_id) can
        # be paired back to the right row, and so merge/delete can
        # find sibling text.
        self._pending_text_by_id: dict[str, str] = {}
        self._last_chunk_id: Optional[str] = None

        # Optional popup badges to mirror status onto -- set by
        # MainWindow while the Advanced Audio popup is open, cleared
        # again when it closes. The popup owns its own StatusBadge
        # instances rather than borrowing these ones.
        self._popup_stt_badge: Optional[StatusBadge] = None
        self._popup_tts_badge: Optional[StatusBadge] = None

        self._build_ui()
        self._wire_signals()
        self._refresh_device_lists()
        self._refresh_profile_list()

    # ── UI construction ──────────────────────────────────────────────────

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

        label = QLabel("VOICE PROFILE")
        label.setObjectName("fieldLabel")
        layout.addWidget(label)

        row = QHBoxLayout()
        self._profile_combo = _make_shrinkable_combo()
        self._profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        row.addWidget(self._profile_combo, stretch=1)

        refresh_profiles_btn = QPushButton("Refresh profiles")
        refresh_profiles_btn.setObjectName("ghost")
        refresh_profiles_btn.clicked.connect(self._refresh_profile_list)
        row.addWidget(refresh_profiles_btn)
        layout.addLayout(row)

        self._reference_file_label = QLabel("No profile selected")
        self._reference_file_label.setObjectName("placeholder")
        # A long resolved filename shouldn't be able to force this row
        # (and the window) wider than the viewport -- let it wrap
        # instead of enforcing a one-line minimum width.
        self._reference_file_label.setWordWrap(True)
        layout.addWidget(self._reference_file_label)

        # Model status line -- shows whether a persistent model exists.
        self._model_status_label = QLabel("")
        self._model_status_label.setObjectName("placeholder")
        self._model_status_label.setWordWrap(True)
        layout.addWidget(self._model_status_label)

        self._create_model_btn = QPushButton("Create persistent voice model")
        self._create_model_btn.setObjectName("ghost")
        self._create_model_btn.clicked.connect(self._create_voice_model)
        self._create_model_btn.setEnabled(False)
        self._create_model_btn.setVisible(False)
        layout.addWidget(self._create_model_btn)

        transcript_label = QLabel(
            "REFERENCE TRANSCRIPT  (what's spoken in the clip above — required for cloning)"
        )
        transcript_label.setObjectName("fieldLabel")
        # Same reasoning as the reference-image label in
        # LiveEditorWidget: without word wrap this long sentence would
        # set the widget's (and window's) minimum width, overflowing
        # the viewport on narrower sizes.
        transcript_label.setWordWrap(True)
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
        self._input_combo = _make_shrinkable_combo()
        input_col.addWidget(self._input_combo)
        layout.addLayout(input_col, 1)

        output_col = QVBoxLayout()
        output_label = QLabel("AUDIO OUTPUT (PLAYBACK)")
        output_label.setObjectName("fieldLabel")
        output_col.addWidget(output_label)
        self._output_combo = _make_shrinkable_combo()
        output_col.addWidget(self._output_combo)
        layout.addLayout(output_col, 1)

        output2_col = QVBoxLayout()
        output2_label = QLabel("ALSO OUTPUT TO (OPTIONAL)")
        output2_label.setObjectName("fieldLabel")
        output2_col.addWidget(output2_label)
        self._output2_combo = _make_shrinkable_combo()
        output2_col.addWidget(self._output2_combo)
        layout.addLayout(output2_col, 1)

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
        self._pipeline_widget = w
        self._pipeline_layout = layout = QVBoxLayout(w)
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

        self._block_list = TextBlockList()
        self._block_list.block_delete_requested.connect(self._on_block_delete_requested)
        self._block_list.block_merge_requested.connect(self._on_block_merge_requested)
        self._block_list.filler_button_clicked.connect(self._on_filler_button_clicked)
        self._block_list.auto_filler_toggled.connect(self._on_auto_filler_toggled)
        layout.addWidget(self._block_list)

        self._error_label = QLabel("")
        self._error_label.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 12px;")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)

        return w

    # ── signal wiring ────────────────────────────────────────────────────

    def _wire_signals(self):
        self._final_text_ready.connect(self._on_final_text_main_thread)
        self._partial_text_ready.connect(self._on_partial_text_main_thread)
        self._stt_status_changed.connect(self._stt_badge.set_status)
        self._tts_status_changed.connect(self._tts_badge.set_status)
        self._stt_status_changed.connect(self._notify_popup_stt_status)
        self._tts_status_changed.connect(self._notify_popup_tts_status)
        self._error_occurred.connect(self._on_error_main_thread)
        self._block_added.connect(self._block_list.add_block)
        self._block_active_changed.connect(self._on_block_active_changed_main_thread)
        self._block_locked_changed.connect(self._block_list.set_locked)
        self._stt_latency_ready.connect(self._block_list.set_stt_latency)
        self._tts_latency_ready.connect(self._on_tts_latency_main_thread)

    # ── collapse toggle ──────────────────────────────────────────────────

    def _on_toggle(self):
        expanded = self._toggle_btn.isChecked()
        self._body.setVisible(expanded)
        arrow = "▾" if expanded else "▸"
        self._toggle_btn.setText(f"{arrow}  ADVANCED · AUDIO (VOICE CLONE)")

    # ── voice profile selection ──────────────────────────────────────────

    def _refresh_profile_list(self):
        previous = self._profile_combo.currentData()
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for name in voice_profiles.list_profile_names():
            self._profile_combo.addItem(name, userData=name)
        self._profile_combo.blockSignals(False)

        if self._profile_combo.count() == 0:
            self._current_profile = None
            self._reference_path = None
            self._reference_file_label.setText(
                "No profiles found -- add a subfolder under profiles/"
            )
            self._reference_transcript_edit.clear()
            self._block_list.set_profile(None)
            self._update_model_status()
            return

        idx = self._profile_combo.findData(previous) if previous else -1
        self._profile_combo.setCurrentIndex(idx if idx >= 0 else 0)
        # setCurrentIndex above only emits currentIndexChanged if the
        # index actually moved, so make sure the profile gets loaded
        # even when the selection lands back on index 0 unchanged.
        self._on_profile_selected(self._profile_combo.currentIndex())

    def _on_profile_selected(self, _index: int):
        name = self._profile_combo.currentData()
        if not name:
            return
        profile = voice_profiles.load_profile(name)
        self._current_profile = profile
        self._block_list.set_profile(profile)

        if profile is None or profile.reference_audio is None:
            self._reference_path = None
            self._reference_file_label.setText(
                f"No reference audio found in profiles/{name}/"
            )
            self._reference_file_label.setObjectName("placeholder")
            self._update_model_status()
            return

        self._reference_path = str(profile.reference_audio)
        self._reference_file_label.setText(profile.reference_audio.name)
        self._reference_file_label.setObjectName("")
        self._reference_file_label.setStyleSheet("color: rgba(255,255,255,0.7);")
        self._reference_transcript_edit.setText(profile.reference_transcript)
        self._update_model_status()
        log.info(
            "Voice profile selected: %s (reference=%s, fillers=%d, model=%s)",
            name,
            profile.reference_audio,
            len(profile.filler_paths),
            profile.reference_id or "(none)",
        )

    def _update_model_status(self):
        """Show model status and the 'create model' button if applicable."""
        profile = self._current_profile
        if profile is None or profile.reference_audio is None:
            self._model_status_label.setText("")
            self._create_model_btn.setVisible(False)
            self._create_model_btn.setEnabled(False)
            return

        if profile.has_persistent_model:
            self._model_status_label.setText(
                f"Persistent model ready (id: {profile.reference_id[:12]}…)"
            )
            self._model_status_label.setStyleSheet("color: rgba(100,255,100,0.7);")
            self._create_model_btn.setVisible(False)
        else:
            self._model_status_label.setText(
                "No persistent model — reference audio will be re-embedded on every "
                "phrase (adds ~3-4s latency). Click below to create a persistent model."
            )
            self._model_status_label.setStyleSheet("color: rgba(255,200,100,0.7);")
            self._create_model_btn.setVisible(True)
            self._create_model_btn.setEnabled(True)

    def _create_voice_model(self):
        """Create a persistent Fish Audio voice model from the reference audio."""
        profile = self._current_profile
        if profile is None or not self._config.fish_key:
            self._show_error("No profile selected or missing Fish API key.")
            return
        if profile.has_persistent_model:
            self._show_error("Model already exists for this profile.")
            return

        self._create_model_btn.setEnabled(False)
        self._create_model_btn.setText("Uploading to Fish Audio…")
        self._error_label.setText("")

        try:
            model_id = voice_profiles.create_fish_model(
                api_key=self._config.fish_key,
                profile=profile,
            )
            # Reload the profile so it picks up the new reference_id.
            self._on_profile_selected(self._profile_combo.currentIndex())
            log.info("Created Fish Audio model: %s", model_id)
            self._show_error("")  # clear any error
        except Exception as exc:
            self._show_error(f"Failed to create model: {exc}")
            self._create_model_btn.setEnabled(True)
            self._create_model_btn.setText("Create persistent voice model")

    # ── device lists ─────────────────────────────────────────────────────

    def _refresh_device_lists(self):
        self._input_combo.clear()
        for d in audio_devices.list_input_devices():
            self._input_combo.addItem(d.name, userData=d.index)

        self._output_combo.clear()
        for d in audio_devices.list_output_devices():
            self._output_combo.addItem(d.name, userData=d.index)

        self._output2_combo.clear()
        self._output2_combo.addItem("None", userData=None)
        for d in audio_devices.list_output_devices():
            self._output2_combo.addItem(d.name, userData=d.index)

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
        # "Also output to" defaults to None -- opt-in, not opt-out.
        self._output2_combo.setCurrentIndex(0)

    # ── pipeline control ─────────────────────────────────────────────────

    def _start_pipeline(self):
        self._error_label.setText("")
        self._block_list.clear()
        self._pending_text_by_id.clear()
        self._last_chunk_id = None

        missing = self._config.missing_for_voice_clone()
        if missing:
            self._show_error(f"Missing API key(s): {', '.join(missing)}. Add them to .env.")
            return
        if not self._reference_path:
            self._show_error(
                "Select a voice profile with a reference audio clip first "
                "(add one under profiles/<name>/ if none are available)."
            )
            return
        if not self._reference_transcript_edit.text().strip():
            self._show_error("Enter the transcript of the reference clip (required for cloning).")
            return

        profile = self._current_profile
        reference_bytes = Path(self._reference_path).read_bytes()
        reference_transcript = self._reference_transcript_edit.text().strip()
        output_device_index = self._output_combo.currentData()
        output2_device_index = self._output2_combo.currentData()
        output_device_indices = [output_device_index]
        if output2_device_index is not None:
            output_device_indices.append(output2_device_index)
        input_device_index = self._input_combo.currentData()

        # Use the persistent model if available; falls back to instant clone.
        reference_id = profile.reference_id if profile and profile.has_persistent_model else None

        log.info(
            "Starting voice-clone pipeline (input device=%s, output devices=%s, model=%s)",
            self._input_combo.currentText(),
            [self._output_combo.currentText(), self._output2_combo.currentText()]
            if output2_device_index is not None
            else [self._output_combo.currentText()],
            reference_id or "instant clone",
        )

        self._speaker = FishVoiceCloneSpeaker(
            api_key=self._config.fish_key,
            reference_audio_bytes=reference_bytes,
            reference_transcript=reference_transcript,
            output_device_indices=output_device_indices,
            on_status=lambda s: self._tts_status_changed.emit(s),
            on_error=lambda e: self._error_occurred.emit(e),
            on_phrase_played=self._on_phrase_played_bg_thread,
            on_active_changed=lambda cid, text: self._block_active_changed.emit(cid, text),
            on_phrase_locked=lambda cid, locked: self._block_locked_changed.emit(cid, locked),
            reference_id=reference_id,
        )
        self._speaker.start()

        self._streamer = AssemblyAIStreamer(
            api_key=self._config.assemblyai_key,
            on_partial_text=lambda t: self._partial_text_ready.emit(t),
            on_final_text=self._on_final_text_bg_thread,
            on_status=lambda s: self._stt_status_changed.emit(s),
            on_error=lambda e: self._error_occurred.emit(e),
            on_pending_count=None,
            on_latency=self._on_stt_latency_bg_thread,
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
        log.info("Stopping voice-clone pipeline")
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
        self._block_list.set_active(None)

    # ── background-thread trampolines (NOT safe to touch widgets here) ───
    # AssemblyAIStreamer/FishVoiceCloneSpeaker invoke these directly from
    # their own worker threads. Each one only re-packages arguments into
    # a Qt signal emit, which is safe to call cross-thread and gets
    # delivered to the matching _..._main_thread slot below on the Qt
    # event loop.

    def _on_final_text_bg_thread(self, text: str, chunk_id: str):
        self._pending_text_by_id[chunk_id] = text
        self._last_chunk_id = chunk_id
        self._block_added.emit(chunk_id, text)
        self._final_text_ready.emit(text)
        if self._speaker:
            self._speaker.enqueue_text(text, chunk_id=chunk_id)

    def _on_stt_latency_bg_thread(self, chunk_text: str, seconds: float):
        # AssemblyAIStreamer reports latency by chunk text, not
        # chunk_id (see its on_latency docstring) -- pair it back to
        # the most recently added block with matching text.
        chunk_id = self._last_chunk_id
        if chunk_id is not None and self._pending_text_by_id.get(chunk_id) == chunk_text:
            self._stt_latency_ready.emit(chunk_id, seconds)

    def _on_phrase_played_bg_thread(self, chunk_id: Optional[str], text: str, latency: float):
        if chunk_id:
            self._tts_latency_ready.emit(chunk_id, latency)
        if self._streamer:
            self._streamer.ack_played()

    # ── main-thread slots (safe to touch widgets here) ───────────────────

    def _on_final_text_main_thread(self, text: str):
        log.info("Chunk ready -> cloning: %s", text)

    def _on_partial_text_main_thread(self, text: str):
        # Lightweight feedback only -- not sent to Fish Audio. The
        # block list shows finalized chunks; partial text isn't
        # surfaced as its own row to avoid a flickering row that keeps
        # getting replaced.
        pass

    def _on_block_active_changed_main_thread(self, chunk_id, text):  # noqa: ANN001
        self._block_list.set_active(chunk_id)

    def _on_tts_latency_main_thread(self, chunk_id: str, seconds: float):
        self._block_list.set_tts_latency(chunk_id, seconds)
        self._block_list.mark_done(chunk_id)
        self._pending_text_by_id.pop(chunk_id, None)

    def _on_block_delete_requested(self, chunk_id: str):
        log.info("Deleting pending text block: %s", chunk_id)
        self._pending_text_by_id.pop(chunk_id, None)
        self._block_list.remove_block(chunk_id)
        if self._speaker:
            self._speaker.cancel_pending(chunk_id)

    def _on_block_merge_requested(self, chunk_id: str):
        log.info("Merging text block into previous: %s", chunk_id)
        self._pending_text_by_id.pop(chunk_id, None)
        self._block_list.merge_block_into_previous(chunk_id)
        if self._speaker:
            self._speaker.cancel_pending(chunk_id)

    def _on_filler_button_clicked(self, wav_path: str):
        if self._speaker:
            self._speaker.play_filler(wav_path)

    def _on_auto_filler_toggled(self, enabled: bool):
        if not self._speaker:
            return
        self._speaker.set_auto_filler(
            enabled,
            filler_resolver=self._resolve_auto_filler if enabled else None,
        )

    def _resolve_auto_filler(self, gap_seconds: Optional[float]) -> Optional[str]:
        # Only fill a gap if there's still a pending block coming --
        # filling silence with nothing left to say would be misleading.
        if not self._block_list.has_pending_blocks():
            return None
        return self._block_list.filler_path_for_gap(gap_seconds)

    def _on_error_main_thread(self, message: str):
        self._show_error(message)

    def _show_error(self, message: str):
        log.error(message)
        self._error_label.setText(message)

    # ── expand-view popup support ────────────────────────────────────────
    # When the Live Editor's output view is expanded (see MainWindow),
    # this panel's transcript/controls move into a separate
    # AdvancedAudioPopup window so they stay usable while the video
    # fills the app viewport. These accessors let MainWindow borrow the
    # widgets it needs without AdvancedAudioWidget knowing anything
    # about the popup itself.

    def stop_pipeline_external(self):
        """Public wrapper so MainWindow's popup Stop button can drive
        the same pipeline shutdown path as the inline Stop button."""
        self._stop_pipeline()

    def is_running(self) -> bool:
        return self._running

    @property
    def block_list(self) -> TextBlockList:
        return self._block_list

    def move_block_list_to(self, target_layout) -> None:
        """Reparent the block list widget into another layout (the
        popup's), remembering the index it came from so it can be
        put back exactly where it was."""
        self._block_list_home_index = self._pipeline_layout.indexOf(self._block_list)
        self._pipeline_layout.removeWidget(self._block_list)
        target_layout.addWidget(self._block_list)

    def set_popup_badges(
        self, stt_badge: Optional[StatusBadge], tts_badge: Optional[StatusBadge]
    ) -> None:
        """Called by MainWindow when the popup opens/closes so status
        updates (idle/listening/speaking/error) reach the popup's own
        badge widgets too, not just the inline ones."""
        self._popup_stt_badge = stt_badge
        self._popup_tts_badge = tts_badge
        if stt_badge is not None:
            stt_badge.set_status(self._stt_badge._status)
        if tts_badge is not None:
            tts_badge.set_status(self._tts_badge._status)

    def _notify_popup_stt_status(self, status: str):
        if self._popup_stt_badge is not None:
            self._popup_stt_badge.set_status(status)

    def _notify_popup_tts_status(self, status: str):
        if self._popup_tts_badge is not None:
            self._popup_tts_badge.set_status(status)

    def restore_block_list(self) -> None:
        """Move the block list back into this panel's own layout, at
        the position it originally occupied."""
        parent_layout = (
            self._block_list.parentWidget().layout()
            if self._block_list.parentWidget()
            else None
        )
        if parent_layout is not None:
            parent_layout.removeWidget(self._block_list)
        idx = getattr(self, "_block_list_home_index", None)
        if idx is None or idx < 0:
            self._pipeline_layout.addWidget(self._block_list)
        else:
            self._pipeline_layout.insertWidget(idx, self._block_list)
        self._block_list.setParent(self)

    # ── cleanup ──────────────────────────────────────────────────────────

    def shutdown(self):
        if self._running:
            self._stop_pipeline()
