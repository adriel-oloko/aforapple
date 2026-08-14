"""Live transcript as a list of discrete, per-chunk "text blocks".

Each finalized STT chunk (see AssemblyAIStreamer.on_final_text) becomes
one row here instead of one line appended to a plain log. That gives us
a stable place to hang the interactive bits the UI needs:

  - a âœ• button that cancels a chunk before Fish Audio has started
    speaking it (feature: delete a pending block)
  - a green âž• button, shown only on rows whose text does NOT start
    with a capital letter, that merges the row into the previous one
    and cancels its own pending generation (feature: merge/undo a
    stray sentence-boundary split)
  - a blue backdrop on whichever row is currently being fetched/played
    by Fish Audio (driven by FishVoiceCloneSpeaker.on_active_changed)
  - a small latency readout per row once both the STT and TTS timings
    for that chunk are known
  - a row of pre-recorded filler-word buttons + an "Auto" toggle that
    injects a filler into dead air between phrases, plus a second row
    below it for longer, phrase-length fillers (e.g. "So um you know
    like basically,")

This widget owns no network/audio state itself -- it's driven entirely
by the callbacks AdvancedAudioWidget wires up to AssemblyAIStreamer and
FishVoiceCloneSpeaker, and its âœ•/âž• actions call back out through
signals so the owner can talk to those services.
"""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.voice_profiles import FILLER_PHRASES, FILLER_TEXTS, VoiceProfile

# Auto-filler picks a single-word clip (FILLER_TEXTS) for gaps shorter than
# this, and a longer hedging-phrase clip (FILLER_PHRASES) for gaps at or
# beyond it -- a bigger silence can absorb a bigger interruption without
# feeling like a stutter. Tunable without a code change since it's a pure
# UX/timing knob.
_LONG_GAP_SECONDS_FOR_PHRASE_FILLER = float(
    os.getenv("FILLER_LONG_GAP_SECONDS", "1.2")
)


class _BlockRow(QFrame):
    """One finalized chunk of speech, pending / active / done."""

    delete_requested = pyqtSignal(str)  # chunk_id
    merge_requested = pyqtSignal(str)  # chunk_id

    def __init__(self, chunk_id: str, text: str, parent=None):
        super().__init__(parent)
        self.chunk_id = chunk_id
        self.text = text
        self._active = False
        self._locked = False
        self._done = False
        self._stt_latency: Optional[float] = None
        self._tts_latency: Optional[float] = None

        self.setObjectName("blockRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        # Green "merge with previous" button -- only visible for rows
        # whose text doesn't start with a capital letter, i.e. it reads
        # like a continuation AssemblyAI split off too eagerly.
        self._merge_btn = QPushButton("+")
        self._merge_btn.setObjectName("mergeBtn")
        self._merge_btn.setFixedSize(22, 22)
        self._merge_btn.setToolTip("Merge into previous block")
        self._merge_btn.clicked.connect(lambda: self.merge_requested.emit(self.chunk_id))
        self._merge_btn.setVisible(not self._starts_with_capital(text))
        layout.addWidget(self._merge_btn)

        self._text_label = QLabel(text)
        self._text_label.setWordWrap(True)
        self._text_label.setObjectName("blockText")
        layout.addWidget(self._text_label, stretch=1)

        self._latency_label = QLabel("")
        self._latency_label.setObjectName("blockLatency")
        self._latency_label.setStyleSheet("color: rgba(255,255,255,0.35); font-size: 10px;")
        layout.addWidget(self._latency_label)

        self._delete_btn = QPushButton("âœ•")
        self._delete_btn.setObjectName("deleteBtn")
        self._delete_btn.setFixedSize(22, 22)
        self._delete_btn.setToolTip("Remove -- won't be spoken")
        self._delete_btn.clicked.connect(lambda: self.delete_requested.emit(self.chunk_id))
        layout.addWidget(self._delete_btn)

        self._apply_style()

    @staticmethod
    def _starts_with_capital(text: str) -> bool:
        stripped = text.strip()
        return bool(stripped) and stripped[0].isupper()

    def set_active(self, active: bool):
        self._active = active
        self._refresh_controls()
        self._apply_style()

    def set_locked(self, locked: bool):
        self._locked = locked
        self._refresh_controls()
        self._apply_style()

    def set_done(self, done: bool):
        self._done = done
        self._refresh_controls()
        self._apply_style()

    def _refresh_controls(self):
        # Once a block is being fetched, playing, or already done, cancelling
        # it would desync the ordered TTS playback queue from the UI.
        enabled = not self._done and not self._active and not self._locked
        self._delete_btn.setEnabled(enabled)
        self._merge_btn.setEnabled(enabled)

    def set_stt_latency(self, seconds: float):
        self._stt_latency = seconds
        self._refresh_latency_label()

    def set_tts_latency(self, seconds: float):
        self._tts_latency = seconds
        self._refresh_latency_label()

    def _refresh_latency_label(self):
        parts = []
        if self._stt_latency is not None:
            parts.append(f"STT {self._stt_latency * 1000:.0f}ms")
        if self._tts_latency is not None:
            parts.append(f"TTS {self._tts_latency * 1000:.0f}ms")
        self._latency_label.setText("  Â·  ".join(parts))

    def _apply_style(self):
        if self._active:
            self.setStyleSheet(
                "QFrame#blockRow {"
                " background-color: rgba(70, 130, 255, 0.22);"
                " border: 1px solid rgba(90, 150, 255, 0.55);"
                "}"
            )
        elif self._done:
            self.setStyleSheet(
                "QFrame#blockRow {"
                " background-color: transparent;"
                " border: 1px solid rgba(255,255,255,0.06);"
                "}"
            )
            self._text_label.setStyleSheet("color: rgba(255,255,255,0.45);")
        elif self._locked:
            self.setStyleSheet(
                "QFrame#blockRow {"
                " background-color: rgba(255,255,255,0.04);"
                " border: 1px solid rgba(255,255,255,0.18);"
                "}"
            )
            self._text_label.setStyleSheet("color: rgba(255,255,255,0.7);")
        else:
            self.setStyleSheet(
                "QFrame#blockRow {"
                " background-color: rgba(255,255,255,0.04);"
                " border: 1px solid rgba(255,255,255,0.10);"
                "}"
            )
            self._text_label.setStyleSheet("color: rgba(255,255,255,0.85);")


class TextBlockList(QWidget):
    """Scrollable list of _BlockRow widgets plus the filler-word row.

    Public signals let the owner (AdvancedAudioWidget) react to user
    actions without this widget knowing anything about
    AssemblyAIStreamer/FishVoiceCloneSpeaker directly.
    """

    block_delete_requested = pyqtSignal(str)  # chunk_id
    block_merge_requested = pyqtSignal(str)  # chunk_id
    filler_button_clicked = pyqtSignal(str)  # wav path
    auto_filler_toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: dict[str, _BlockRow] = {}
        self._order: list[str] = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        label = QLabel("LIVE TRANSCRIPT")
        label.setObjectName("fieldLabel")
        layout.addWidget(label)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFixedHeight(220)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Rows word-wrap their text already; this just makes sure a
        # stray wide row can never sprout a horizontal scrollbar.
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        self._list_layout.addStretch(1)

        self._placeholder = QLabel(
            "Finalized speech will appear here, one block per phrase."
        )
        self._placeholder.setObjectName("placeholder")
        self._placeholder.setWordWrap(True)
        self._list_layout.insertWidget(0, self._placeholder)

        self._scroll.setWidget(self._list_container)
        layout.addWidget(self._scroll)

        # Filler-word row(s)
        filler_label = QLabel("FILLER WORDS")
        filler_label.setObjectName("fieldLabel")
        layout.addWidget(filler_label)

        # Buttons are created once (order matches FILLER_TEXTS /
        # FILLER_PHRASES) and then re-pointed at whichever profile's clips
        # are current -- see set_profile(). The target path is unknown
        # until a profile is selected, so they all start disabled.
        self._filler_buttons: dict[str, QPushButton] = {}
        self._filler_paths: dict[str, Path] = {}

        words_row = self._build_filler_row(FILLER_TEXTS)
        self._auto_btn = QPushButton("Auto")
        self._auto_btn.setObjectName("secondary")
        self._auto_btn.setCheckable(True)
        self._auto_btn.setToolTip(
            "Automatically inject a filler word into silent gaps between phrases"
        )
        self._auto_btn.toggled.connect(self._on_auto_toggled)
        words_row.addWidget(self._auto_btn)
        layout.addLayout(words_row)

        # Phrase-length fillers get their own block below the single
        # words, wrapped to three buttons per row (see
        # _build_filler_rows) rather than one long row.
        phrases_rows = self._build_filler_rows(FILLER_PHRASES, per_row=3)
        layout.addLayout(phrases_rows)

    def _build_filler_row(self, texts: list[str]) -> QHBoxLayout:
        """Build one row of filler buttons for the given texts, registering
        each in self._filler_buttons. Caller adds the returned layout."""
        row = QHBoxLayout()
        row.setSpacing(6)
        for text in texts:
            btn = QPushButton(text)
            btn.setObjectName("secondary")
            btn.setEnabled(False)
            btn.setToolTip("Select a voice profile with this filler clip")
            btn.clicked.connect(lambda _checked, t=text: self._on_filler_clicked(t))
            row.addWidget(btn)
            self._filler_buttons[text] = btn
        row.addStretch(1)
        return row

    def _build_filler_rows(self, texts: list[str], per_row: int) -> QVBoxLayout:
        """Like _build_filler_row, but wraps to a new QHBoxLayout every
        `per_row` buttons instead of putting all of them in one row --
        used for the (longer) phrase-length fillers so a handful of
        long phrases doesn't force one very wide, overflowing row."""
        rows = QVBoxLayout()
        rows.setSpacing(6)
        for start in range(0, len(texts), per_row):
            rows.addLayout(self._build_filler_row(texts[start : start + per_row]))
        return rows

    # â”€â”€ public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def add_block(self, chunk_id: str, text: str):
        self._placeholder.setVisible(False)
        row = _BlockRow(chunk_id, text)
        row.delete_requested.connect(self.block_delete_requested)
        row.merge_requested.connect(self.block_merge_requested)
        # Insert before the trailing stretch.
        self._list_layout.insertWidget(self._list_layout.count() - 1, row)
        self._rows[chunk_id] = row
        self._order.append(chunk_id)
        self._scroll_to_bottom()

    def remove_block(self, chunk_id: str):
        row = self._rows.pop(chunk_id, None)
        if row is None:
            return
        self._order.remove(chunk_id)
        row.setParent(None)
        row.deleteLater()
        if not self._rows:
            self._placeholder.setVisible(True)

    def merge_block_into_previous(self, chunk_id: str):
        """Visually fold `chunk_id`'s text onto the row right before it
        in display order, then remove the row. The caller is
        responsible for actually cancelling the chunk's TTS via
        FishVoiceCloneSpeaker -- this just updates the list."""
        if chunk_id not in self._rows:
            return
        idx = self._order.index(chunk_id)
        if idx == 0:
            # Nothing to merge into -- just drop it.
            self.remove_block(chunk_id)
            return
        prev_id = self._order[idx - 1]
        prev_row = self._rows.get(prev_id)
        row = self._rows[chunk_id]
        if prev_row is not None:
            merged = f"{prev_row.text.rstrip()} {row.text.lstrip()}"
            prev_row.text = merged
            prev_row._text_label.setText(merged)
        self.remove_block(chunk_id)

    def set_active(self, chunk_id: Optional[str]):
        for cid, row in self._rows.items():
            row.set_active(cid == chunk_id)

    def mark_done(self, chunk_id: str):
        row = self._rows.get(chunk_id)
        if row:
            row.set_done(True)

    def set_locked(self, chunk_id: str, locked: bool):
        row = self._rows.get(chunk_id)
        if row:
            row.set_locked(locked)

    def set_stt_latency(self, chunk_id: str, seconds: float):
        row = self._rows.get(chunk_id)
        if row:
            row.set_stt_latency(seconds)

    def set_tts_latency(self, chunk_id: str, seconds: float):
        row = self._rows.get(chunk_id)
        if row:
            row.set_tts_latency(seconds)

    def has_pending_blocks(self) -> bool:
        """True if any block hasn't finished playing yet -- used to
        gate Auto-filler (no point filling a gap if nothing's coming)."""
        return any(not r._done for r in self._rows.values())

    def set_auto_filler_checked(self, checked: bool):
        self._auto_btn.blockSignals(True)
        self._auto_btn.setChecked(checked)
        self._auto_btn.blockSignals(False)

    def clear(self):
        for cid in list(self._order):
            self.remove_block(cid)

    def set_profile(self, profile: Optional[VoiceProfile]):
        """Point the filler buttons at the given profile's clips (or,
        if None, disable them all). Called by AdvancedAudioWidget
        whenever the voice-profile dropdown selection changes."""
        self._filler_paths = dict(profile.filler_paths) if profile else {}
        for text, btn in self._filler_buttons.items():
            path = self._filler_paths.get(text)
            btn.setEnabled(path is not None)
            btn.setToolTip(
                "Play this filler now"
                if path is not None
                else "This profile has no clip for this filler"
            )

    def filler_path_for_gap(self, gap_seconds: Optional[float]) -> Optional[str]:
        """Pick a filler clip sized to the gap it needs to cover.

        Gaps shorter than ``_LONG_GAP_SECONDS_FOR_PHRASE_FILLER`` prefer a
        single-word clip (FILLER_TEXTS) -- a quick "um" reads as live
        thinking, while a whole hedging phrase would be a bigger
        interruption than a short gap calls for. Gaps at or beyond the
        threshold prefer a phrase clip (FILLER_PHRASES) instead, since a
        lone "um" repeated to fill a long silence starts to sound looped.
        If the preferred tier has no clip for the current profile, falls
        back to the other tier. Within a tier, picks randomly among the
        clips the profile actually has so the same filler doesn't repeat
        back-to-back.
        """
        prefer_phrase = (
            gap_seconds is not None
            and gap_seconds >= _LONG_GAP_SECONDS_FOR_PHRASE_FILLER
        )
        primary = FILLER_PHRASES if prefer_phrase else FILLER_TEXTS
        fallback = FILLER_TEXTS if prefer_phrase else FILLER_PHRASES

        path = self._random_available_filler(primary)
        if path is not None:
            return path
        return self._random_available_filler(fallback)

    def _random_available_filler(self, candidates: list[str]) -> Optional[str]:
        available = [
            self._filler_paths[text]
            for text in candidates
            if text in self._filler_paths
        ]
        if not available:
            return None
        return str(random.choice(available))

    # â”€â”€ internal â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _on_auto_toggled(self, checked: bool):
        self.auto_filler_toggled.emit(checked)

    def _on_filler_clicked(self, text: str):
        path = self._filler_paths.get(text)
        if path is not None:
            self.filler_button_clicked.emit(str(path))

    def _scroll_to_bottom(self):
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())


