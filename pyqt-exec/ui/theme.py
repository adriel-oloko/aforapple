"""Shared dark theme (QSS) matching the original Next.js app's look:
black background, hairline white/10-20% borders, square corners,
uppercase tracked-out labels, minimal chrome.
"""

DARK_QSS = """
QWidget {
    background-color: #000000;
    color: rgba(255, 255, 255, 0.9);
    font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
}

QMainWindow {
    background-color: #000000;
}

/* Section / group frames -- hairline border, no radius */
QFrame#panel, QFrame#videoBox, QFrame#sectionFrame {
    border: 1px solid rgba(255, 255, 255, 0.10);
    background-color: #000000;
}

QLabel#sectionTitle {
    font-size: 16px;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.90);
}

QLabel#sectionSubtitle {
    font-size: 11px;
    color: rgba(255, 255, 255, 0.35);
}

QLabel#fieldLabel {
    font-size: 11px;
    font-weight: 500;
    color: rgba(255, 255, 255, 0.35);
    text-transform: uppercase;
}

QLabel#placeholder {
    color: rgba(255, 255, 255, 0.25);
    font-size: 12px;
}

/* Primary action button -- solid white, black text (mirrors the JS "Apply"/"Start" button) */
QPushButton#primary {
    background-color: #ffffff;
    color: #000000;
    border: 1px solid rgba(255, 255, 255, 0.15);
    padding: 8px 16px;
    font-weight: 500;
}
QPushButton#primary:hover {
    background-color: rgba(255, 255, 255, 0.90);
}
QPushButton#primary:disabled {
    background-color: rgba(255, 255, 255, 0.15);
    color: rgba(0, 0, 0, 0.4);
}

/* Secondary button -- outline only (mirrors the JS "Stop"/"Expand" button) */
QPushButton#secondary {
    background-color: transparent;
    color: rgba(255, 255, 255, 0.70);
    border: 1px solid rgba(255, 255, 255, 0.15);
    padding: 8px 16px;
}
QPushButton#secondary:hover {
    background-color: rgba(255, 255, 255, 0.05);
    border-color: rgba(255, 255, 255, 0.25);
}
QPushButton#secondary:disabled {
    color: rgba(255, 255, 255, 0.15);
}

QPushButton#ghost {
    background-color: transparent;
    color: rgba(255, 255, 255, 0.45);
    border: none;
    text-decoration: underline;
    font-size: 11px;
}
QPushButton#ghost:hover {
    color: rgba(255, 255, 255, 0.70);
}

QTextEdit, QPlainTextEdit, QLineEdit {
    background-color: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: rgba(255, 255, 255, 0.80);
    padding: 6px 10px;
    selection-background-color: rgba(255, 255, 255, 0.25);
}
QTextEdit:focus, QLineEdit:focus {
    border: 1px solid rgba(255, 255, 255, 0.35);
}

QComboBox {
    background-color: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: rgba(255, 255, 255, 0.80);
    padding: 6px 10px;
}
QComboBox:hover {
    border-color: rgba(255, 255, 255, 0.25);
}
QComboBox QAbstractItemView {
    background-color: #0a0a0a;
    color: rgba(255, 255, 255, 0.80);
    border: 1px solid rgba(255, 255, 255, 0.15);
    selection-background-color: rgba(255, 255, 255, 0.15);
}

QScrollArea {
    border: none;
    background-color: transparent;
}

QToolButton#collapseHeader {
    background-color: transparent;
    border: none;
    color: rgba(255, 255, 255, 0.70);
    font-size: 11px;
    font-weight: 600;
    text-align: left;
    padding: 4px 0;
}

QSplitter::handle {
    background-color: rgba(255, 255, 255, 0.08);
}
"""

# Status dot colors, mirroring StatusBadge's dotClass map in the original TSX
STATUS_COLORS = {
    "idle": "rgba(255,255,255,0.20)",
    "requesting": "rgba(255,255,255,0.45)",
    "connecting": "rgba(255,255,255,0.45)",
    "live": "rgba(255,255,255,1.0)",
    "listening": "rgba(255,255,255,1.0)",
    "speaking": "rgba(255,255,255,1.0)",
    "error": "rgba(255,255,255,0.55)",
}

STATUS_LABELS = {
    "idle": "Idle",
    "requesting": "Requesting camera…",
    "connecting": "Connecting…",
    "live": "Live",
    "listening": "Listening…",
    "speaking": "Speaking…",
    "error": "Error",
}
