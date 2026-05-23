#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JARVIS — Glassmorphism UI                                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝

HOW TO RUN
  ollama serve                  # keep running in a separate terminal
  source .venv/bin/activate
  python app.py
"""

import os
import re
import sys
import time
import wave
import queue
import tempfile
import threading
import traceback
import numpy as np
import sounddevice as sd
import whisper
import ollama
import openwakeword
from openwakeword.model import Model
from dotenv import load_dotenv

from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QPropertyAnimation, QRect, QSize,
    QPoint, QTimer, QEasingCurve
)
from PyQt6.QtGui import (
    QPainter, QPainterPath, QColor, QLinearGradient, QRadialGradient,
    QPen, QBrush, QFont
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QScrollArea, QVBoxLayout,
    QHBoxLayout, QLineEdit, QPushButton, QFrame, QSizePolicy
)

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

WHISPER_MODEL   = os.getenv("WHISPER_MODEL", "base")
WAKE_THRESHOLD  = float(os.getenv("WAKE_THRESHOLD", "0.5"))
OLLAMA_MODEL    = os.getenv("OLLAMA_MODEL", "llama3.2")

_raw        = os.getenv("JARVIS_WAKE_WORDS", "hey_jarvis")
WAKE_WORDS  = [w.strip() for w in _raw.split(",") if w.strip()]
AVAILABLE   = ["hey_jarvis", "alexa", "hey_mycroft", "ok_nabu"]

_bad = [w for w in WAKE_WORDS if w not in AVAILABLE]
if _bad:
    print(f"Unknown wake word(s): {_bad}. Choose from: {AVAILABLE}")
    sys.exit(1)

SAMPLE_RATE     = 16000
CHUNK_SIZE      = 1280
SILENCE_SEC     = 1.5
MIN_SPEECH_SEC  = 0.3
MAX_RECORD_SEC  = 15
EXIT_PHRASE     = "thank you jarvis"

_SILENCE_CHUNKS = int(SILENCE_SEC * SAMPLE_RATE / CHUNK_SIZE)
_MAX_CHUNKS     = int(MAX_RECORD_SEC * SAMPLE_RATE / CHUNK_SIZE)

SYSTEM_PROMPT = (
    "You are Jarvis, a concise and helpful personal AI assistant running locally on macOS. "
    "Keep answers brief and conversational unless the user asks for detail. "
    "Never say you're an AI or mention your model name."
)

# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def normalize(text: str) -> str:
    return re.sub(r"[^a-z\s]", "", text.lower()).strip()

def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2) + 1e-9))

# ══════════════════════════════════════════════════════════════════════════════
#  VOICE THREAD
# ══════════════════════════════════════════════════════════════════════════════

class VoiceThread(QThread):
    status = pyqtSignal(str)
    woke   = pyqtSignal(float)
    heard  = pyqtSignal(str)
    token  = pyqtSignal(str)
    done   = pyqtSignal()
    err    = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._stop         = threading.Event()
        self._msg_queue    = queue.Queue()
        self._inject_queue = queue.Queue()
        self._cb           = {
            "mode": "idle", "chunks": [], "silence_cnt": 0,
            "last_trig": 0.0, "rec_start": 0.0,
            "silence_thr": 0.03, "oww": None,
        }
        self._history = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._stt = None

    def stop(self):
        self._stop.set()

    def send_text(self, text: str):
        self._inject_queue.put(text)

    # ── audio callback ────────────────────────────────────────────────────────

    def _audio_cb(self, indata, _frames, _time_info, _status):
        if self._stop.is_set():
            return
        try:
            chunk = indata.flatten().copy()
            level = rms(chunk)
            cb    = self._cb

            if cb["mode"] == "idle":
                if cb["oww"] is None:
                    return
                preds = cb["oww"].predict(chunk)
                score = max((preds.get(w, 0.0) for w in WAKE_WORDS), default=0.0)
                now   = time.time()
                if score >= WAKE_THRESHOLD and (now - cb["last_trig"]) > 1.0:
                    cb.update(mode="recording", chunks=[], silence_cnt=0,
                              last_trig=now, rec_start=now)
                    self._msg_queue.put(("wake", score))
            else:
                cb["chunks"].append(chunk)
                cb["silence_cnt"] = cb["silence_cnt"] + 1 if level <= cb["silence_thr"] else 0
                if cb["silence_cnt"] >= _SILENCE_CHUNKS or len(cb["chunks"]) >= _MAX_CHUNKS:
                    audio    = np.concatenate(cb["chunks"])
                    duration = time.time() - cb["rec_start"]
                    cb["mode"]   = "idle"
                    cb["chunks"] = []
                    self._msg_queue.put(("audio", audio, duration))
        except Exception as e:
            pass  # callback must never raise

    # ── calibration ───────────────────────────────────────────────────────────

    def _calibrate(self) -> float:
        self.status.emit("Calibrating mic…")
        levels, done_ev = [], threading.Event()
        n = max(1, int(SAMPLE_RATE / CHUNK_SIZE))

        def _cb(indata, _f, _t, _s):
            levels.append(rms(indata.flatten()))
            if len(levels) >= n:
                done_ev.set()

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=CHUNK_SIZE, callback=_cb):
            done_ev.wait(timeout=5.0)

        ambient = float(np.mean(levels)) if levels else 0.03
        return max(0.02, ambient * 2.5)

    # ── transcription ─────────────────────────────────────────────────────────

    def _transcribe(self, audio: np.ndarray) -> str:
        if len(audio) < SAMPLE_RATE * MIN_SPEECH_SEC:
            return ""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        try:
            with wave.open(tmp, "w") as wf:
                wf.setnchannels(1); wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE); wf.writeframes(audio.tobytes())
            return self._stt.transcribe(tmp, language="en", fp16=False)["text"].strip()
        except Exception as e:
            self.err.emit(f"Whisper error: {e}")
            return ""
        finally:
            os.unlink(tmp)

    # ── ollama ────────────────────────────────────────────────────────────────

    def _ask(self, text: str):
        self._history.append({"role": "user", "content": text})
        self.status.emit("Thinking…")
        full = ""
        try:
            for chunk in ollama.chat(model=OLLAMA_MODEL, messages=self._history, stream=True):
                t = chunk["message"]["content"]
                self.token.emit(t)
                full += t
        except ollama.ResponseError as e:
            self.err.emit(f"Ollama error: {e}")
        except Exception as e:
            self.err.emit(f"Ollama unreachable. Run: ollama serve")
        if full:
            self._history.append({"role": "assistant", "content": full})
        self.done.emit()
        self.status.emit("Listening…")

    def _handle(self, text: str):
        self.heard.emit(text)
        if EXIT_PHRASE in normalize(text):
            self._ask("The user is saying goodbye. Say a brief farewell.")
            self._stop.set()
            return
        clean = normalize(text)
        greeting = (
            any(g in clean for g in ("good morning", "good afternoon", "good evening", "good night"))
            and "jarvis" in clean
        )
        if greeting:
            h = time.localtime().tm_hour
            tod = ("morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17
                   else "evening" if 17 <= h < 22 else "night")
            self._ask(f"The user greeted you with '{text}'. It is {tod}. Warm brief greeting.")
        else:
            self._ask(text)

    # ── main loop ─────────────────────────────────────────────────────────────

    def run(self):
        try:
            self._run_inner()
        except Exception as e:
            self.err.emit(f"Voice thread crashed: {e}\n{traceback.format_exc()}")

    def _run_inner(self):
        self._cb["silence_thr"] = self._calibrate()

        self.status.emit("Loading Whisper…")
        self._stt = whisper.load_model(WHISPER_MODEL)

        self.status.emit("Loading wake word model…")
        openwakeword.utils.download_models()
        self._cb["oww"] = Model(wakeword_models=WAKE_WORDS, inference_framework="onnx")

        self.status.emit("Checking Ollama…")
        try:
            available = [m.model for m in ollama.list().models]
            if not any(OLLAMA_MODEL in m for m in available):
                self.err.emit(f"Model '{OLLAMA_MODEL}' not found — run: ollama pull {OLLAMA_MODEL}")
                return
        except Exception as e:
            self.err.emit(f"Ollama not running — start it with: ollama serve")
            return

        self.status.emit("Listening…")

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=CHUNK_SIZE, callback=self._audio_cb):
            while not self._stop.is_set():
                try:
                    text = self._inject_queue.get_nowait()
                    self._handle(text)
                    continue
                except queue.Empty:
                    pass

                try:
                    msg = self._msg_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if msg[0] == "wake":
                    self.woke.emit(msg[1])
                    self.status.emit("Recording…")
                elif msg[0] == "audio":
                    _, audio, _dur = msg
                    if len(audio) < SAMPLE_RATE * MIN_SPEECH_SEC:
                        self.status.emit("Listening…")
                        continue
                    self.status.emit("Transcribing…")
                    text = self._transcribe(audio)
                    if not text:
                        self.status.emit("Listening…")
                        continue
                    self._handle(text)


# ══════════════════════════════════════════════════════════════════════════════
#  CHAT BUBBLE
# ══════════════════════════════════════════════════════════════════════════════

class ChatBubble(QFrame):
    def __init__(self, is_user: bool, parent=None):
        super().__init__(parent)
        self._is_user = is_user
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._label.setStyleSheet(f"""
            color: {"rgba(255,255,255,0.95)" if is_user else "rgba(230,225,255,0.85)"};
            font-size: 14px;
            font-family: -apple-system, 'SF Pro Text', Helvetica, sans-serif;
            line-height: 1.6;
            background: transparent;
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 11, 16, 11)
        lay.addWidget(self._label)

    def set_text(self, text: str):
        self._label.setText(text)

    def append(self, token: str):
        self._label.setText(self._label.text() + token)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 18, 18)

        if self._is_user:
            # Purple-pink gradient
            grad = QLinearGradient(0, 0, self.width(), self.height())
            grad.setColorAt(0, QColor(110, 60, 180, 220))
            grad.setColorAt(1, QColor(160, 50, 130, 220))
            p.fillPath(path, grad)
            p.setPen(QPen(QColor(200, 150, 255, 60), 1))
        else:
            p.fillPath(path, QColor(255, 255, 255, 12))
            p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.drawPath(path)

        # Top highlight
        hi = QPainterPath()
        hi.addRoundedRect(1, 1, self.width() - 2, 20, 17, 17)
        p.fillPath(hi, QColor(255, 255, 255, 8 if not self._is_user else 15))


# ══════════════════════════════════════════════════════════════════════════════
#  CHAT AREA
# ══════════════════════════════════════════════════════════════════════════════

class ChatArea(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setWidgetResizable(True)
        self.setStyleSheet("""
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical {
                background: transparent; width: 4px; border-radius: 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.15); border-radius: 2px; min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._lay = QVBoxLayout(self._container)
        self._lay.setSpacing(12)
        self._lay.setContentsMargins(20, 24, 20, 20)
        self._lay.addStretch()
        self.setWidget(self._container)
        self._jarvis_bubble: ChatBubble | None = None

    def add_user(self, text: str):
        b = ChatBubble(is_user=True)
        b.set_text(text)
        b.setMaximumWidth(480)
        b.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(b)
        w = QWidget(); w.setStyleSheet("background: transparent;"); w.setLayout(row)
        self._lay.insertWidget(self._lay.count() - 1, w)
        self._scroll_bottom()

    def start_jarvis(self):
        self._jarvis_bubble = ChatBubble(is_user=False)
        self._jarvis_bubble.setMaximumWidth(540)
        self._jarvis_bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        row = QHBoxLayout()
        row.addWidget(self._jarvis_bubble)
        row.addStretch()
        w = QWidget(); w.setStyleSheet("background: transparent;"); w.setLayout(row)
        self._lay.insertWidget(self._lay.count() - 1, w)
        self._scroll_bottom()

    def append_token(self, token: str):
        if self._jarvis_bubble:
            self._jarvis_bubble.append(token)
            self._scroll_bottom()

    def end_jarvis(self):
        self._jarvis_bubble = None

    def _scroll_bottom(self):
        QTimer.singleShot(30, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()))


# ══════════════════════════════════════════════════════════════════════════════
#  FLOATING ISLAND
# ══════════════════════════════════════════════════════════════════════════════

_COMPACT  = QSize(260, 46)
_EXPANDED = QSize(420, 110)


class FloatingIsland(QWidget):
    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)

        self._dot_color = QColor(140, 140, 140, 180)
        self._expanded  = False

        self._status_lbl = QLabel("Initializing…")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setStyleSheet("""
            color: rgba(255,255,255,0.88);
            font-size: 13px; font-weight: 500;
            font-family: -apple-system, 'SF Pro Text', Helvetica, sans-serif;
            background: transparent;
            letter-spacing: 0.3px;
        """)

        self._heard_lbl = QLabel("")
        self._heard_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._heard_lbl.setWordWrap(True)
        self._heard_lbl.setStyleSheet("""
            color: rgba(200,180,255,0.65);
            font-size: 12px;
            font-family: -apple-system, 'SF Pro Text', Helvetica, sans-serif;
            background: transparent;
        """)
        self._heard_lbl.setVisible(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(30, 8, 20, 8)
        lay.setSpacing(5)
        lay.addStretch()
        lay.addWidget(self._status_lbl)
        lay.addWidget(self._heard_lbl)
        lay.addStretch()

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(
            (screen.width() - _COMPACT.width()) // 2, 16,
            _COMPACT.width(), _COMPACT.height()
        )
        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def set_status(self, status: str):
        self._status_lbl.setText(status)
        if "Record" in status:
            self._dot_color = QColor(255, 65, 65, 255)
        elif "Think" in status or "Transcrib" in status:
            self._dot_color = QColor(255, 185, 40, 255)
        elif "Listen" in status:
            self._dot_color = QColor(60, 220, 140, 255)
        elif "Error" in status or "error" in status:
            self._dot_color = QColor(255, 80, 80, 255)
        else:
            self._dot_color = QColor(140, 140, 160, 200)
        self.update()

    def set_heard(self, text: str):
        short = text[:55] + "…" if len(text) > 55 else text
        self._heard_lbl.setText(f'"{short}"')

    def _animate(self, expand: bool):
        if self._expanded == expand:
            return
        self._expanded = expand
        self._heard_lbl.setVisible(expand)
        screen = QApplication.primaryScreen().geometry()
        sz = _EXPANDED if expand else _COMPACT
        target = QRect((screen.width() - sz.width()) // 2, 16, sz.width(), sz.height())
        self._anim.stop()
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(target)
        self._anim.start()

    def enterEvent(self, e): self._animate(True);  super().enterEvent(e)
    def leaveEvent(self, e): self._animate(False); super().leaveEvent(e)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        rad = r.height() / 2

        # Pill body — very dark glass
        pill = QPainterPath()
        pill.addRoundedRect(0, 0, r.width(), r.height(), rad, rad)
        p.fillPath(pill, QColor(6, 4, 12, 230))

        # Inner top highlight
        hi = QPainterPath()
        hi.addRoundedRect(1, 1, r.width() - 2, r.height() * 0.55, rad - 1, rad - 1)
        p.fillPath(hi, QColor(255, 255, 255, 10))

        # Border
        p.setPen(QPen(QColor(255, 255, 255, 45), 1))
        p.drawPath(pill)

        # Status dot
        d = 10
        dx, dy = 16, (r.height() - d) // 2
        # Outer glow
        glow_r = QRadialGradient(dx + d/2, dy + d/2, d * 1.8)
        gc = QColor(self._dot_color); gc.setAlpha(70)
        glow_r.setColorAt(0, gc); gc2 = QColor(gc); gc2.setAlpha(0)
        glow_r.setColorAt(1, gc2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow_r))
        p.drawEllipse(int(dx - d), int(dy - d), d * 4, d * 4)
        # Dot itself
        p.setBrush(QBrush(self._dot_color))
        p.drawEllipse(dx, dy, d, d)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN WINDOW
# ══════════════════════════════════════════════════════════════════════════════

SIDEBAR_W = 196


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(820, 560)
        self.resize(960, 640)

        self._drag_pos = QPoint()
        self._voice: VoiceThread | None = None

        self._build()
        self._center()

    def _center(self):
        g = QApplication.primaryScreen().geometry()
        self.move((g.width() - self.width()) // 2, (g.height() - self.height()) // 2)

    # ── construction ─────────────────────────────────────────────────────────

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._sidebar())
        root.addLayout(self._main_col(), 1)

    def _sidebar(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(SIDEBAR_W)
        w.setStyleSheet("background: transparent;")

        lay = QVBoxLayout(w)
        lay.setContentsMargins(18, 30, 18, 24)
        lay.setSpacing(4)

        logo = QLabel("JARVIS")
        logo.setStyleSheet("""
            color: rgba(255,255,255,0.90);
            font-size: 16px; font-weight: 700; letter-spacing: 4px;
            font-family: -apple-system, 'SF Pro Display', Helvetica, sans-serif;
            padding: 4px 10px 18px 10px;
            background: transparent;
        """)
        lay.addWidget(logo)

        for label, active in [("Chat", True), ("History", False), ("Settings", False)]:
            btn = QPushButton(label)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(38)
            if active:
                btn.setStyleSheet("""
                    QPushButton {
                        color: rgba(255,255,255,0.92);
                        background: rgba(130,80,220,0.22);
                        border: 1px solid rgba(180,120,255,0.25);
                        border-radius: 10px;
                        font-size: 13px;
                        font-family: -apple-system, 'SF Pro Text', sans-serif;
                        padding-left: 14px; text-align: left;
                    }
                """)
            else:
                btn.setStyleSheet("""
                    QPushButton {
                        color: rgba(255,255,255,0.38);
                        background: transparent; border: none;
                        border-radius: 10px; font-size: 13px;
                        font-family: -apple-system, 'SF Pro Text', sans-serif;
                        padding-left: 14px; text-align: left;
                    }
                    QPushButton:hover {
                        color: rgba(255,255,255,0.68);
                        background: rgba(255,255,255,0.06);
                    }
                """)
            lay.addWidget(btn)

        lay.addStretch()

        # Loading/status
        self._status_lbl = QLabel("Starting up…")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet("""
            color: rgba(180,150,255,0.45);
            font-size: 11px;
            font-family: -apple-system, 'SF Pro Text', sans-serif;
            padding: 4px 10px;
            background: transparent;
        """)
        lay.addWidget(self._status_lbl)
        return w

    def _main_col(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        # Title bar
        tb = QWidget(); tb.setFixedHeight(52); tb.setStyleSheet("background: transparent;")
        tbl = QHBoxLayout(tb); tbl.setContentsMargins(18, 0, 18, 0); tbl.setSpacing(0)

        close = self._dot("#FF5F57"); close.clicked.connect(QApplication.quit)
        mini  = self._dot("#FEBC2E"); mini.clicked.connect(self.showMinimized)
        maxi  = self._dot("#28C840")
        for d in (close, mini, maxi):
            tbl.addWidget(d); tbl.addSpacing(7)
        tbl.addSpacing(6)

        chat_title = QLabel("Chat")
        chat_title.setStyleSheet("""
            color: rgba(255,255,255,0.70); font-size: 14px; font-weight: 600;
            font-family: -apple-system, 'SF Pro Text', sans-serif;
            background: transparent;
        """)
        tbl.addWidget(chat_title)
        tbl.addStretch()
        col.addWidget(tb)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine); sep.setFixedHeight(1)
        sep.setStyleSheet("background: rgba(255,255,255,0.06);")
        col.addWidget(sep)

        self._chat = ChatArea()
        col.addWidget(self._chat, 1)

        # Input bar
        ib = QWidget(); ib.setFixedHeight(72); ib.setStyleSheet("background: transparent;")
        ibl = QHBoxLayout(ib); ibl.setContentsMargins(18, 14, 18, 14); ibl.setSpacing(10)

        self._input = QLineEdit()
        self._input.setPlaceholderText('Type a message or say "hey jarvis"…')
        self._input.setStyleSheet("""
            QLineEdit {
                background: rgba(255,255,255,0.06);
                border: 1px solid rgba(255,255,255,0.10);
                border-radius: 22px;
                color: rgba(255,255,255,0.90);
                font-size: 14px;
                font-family: -apple-system, 'SF Pro Text', sans-serif;
                padding: 0 20px;
                selection-background-color: rgba(130,80,220,0.5);
            }
            QLineEdit:focus {
                border: 1px solid rgba(180,120,255,0.35);
                background: rgba(130,80,220,0.10);
            }
        """)
        self._input.returnPressed.connect(self._send)
        ibl.addWidget(self._input)

        send = QPushButton("↑")
        send.setFixedSize(44, 44)
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                    stop:0 rgba(130,60,220,230), stop:1 rgba(80,180,255,220));
                border: none; border-radius: 22px;
                color: white; font-size: 20px; font-weight: bold;
            }
            QPushButton:hover  { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,
                stop:0 rgba(150,80,240,255), stop:1 rgba(100,200,255,255)); }
            QPushButton:pressed{ background: rgba(80,40,140,255); }
        """)
        send.clicked.connect(self._send)
        ibl.addWidget(send)
        col.addWidget(ib)
        return col

    @staticmethod
    def _dot(color: str) -> QPushButton:
        b = QPushButton(); b.setFixedSize(13, 13)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(f"QPushButton{{background:{color};border-radius:6px;border:none;}}")
        return b

    # ── send ─────────────────────────────────────────────────────────────────

    def _send(self):
        text = self._input.text().strip()
        if not text or not self._voice:
            return
        self._input.clear()
        self._voice.send_text(text)

    def set_voice(self, v: VoiceThread):
        self._voice = v

    # ── slots ─────────────────────────────────────────────────────────────────

    def on_status(self, s: str):
        self._status_lbl.setText(s)

    def on_heard(self, text: str):
        self._chat.add_user(text)
        self._chat.start_jarvis()

    def on_token(self, t: str):
        self._chat.append_token(t)

    def on_done(self):
        self._chat.end_jarvis()

    def on_error(self, msg: str):
        self._chat.end_jarvis()
        self._status_lbl.setText(f"⚠ {msg}")
        self._status_lbl.setStyleSheet("""
            color: rgba(255,100,100,0.75); font-size: 11px;
            font-family: -apple-system, 'SF Pro Text', sans-serif;
            padding: 4px 10px; background: transparent;
        """)

    # ── drag ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.MouseButton.LeftButton and not self._drag_pos.isNull():
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    # ── painting ──────────────────────────────────────────────────────────────

    @staticmethod
    def _orb(p: QPainter, cx: float, cy: float, r: float, color: QColor):
        """Draw a soft glowing orb — the key element of glassmorphism backgrounds."""
        grad = QRadialGradient(cx, cy, r)
        inner = QColor(color); inner.setAlpha(min(255, color.alpha()))
        outer = QColor(color); outer.setAlpha(0)
        grad.setColorAt(0, inner)
        grad.setColorAt(0.5, QColor(color.red(), color.green(), color.blue(), color.alpha() // 3))
        grad.setColorAt(1, outer)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(int(cx - r), int(cy - r), int(r * 2), int(r * 2))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        clip = QPainterPath()
        clip.addRoundedRect(0, 0, w, h, 20, 20)
        p.setClipPath(clip)

        # Near-black base
        p.fillRect(0, 0, w, h, QColor(5, 3, 12))

        # Glowing orbs — these bleed through the glass for the glassmorphism look
        self._orb(p, w * 0.12, h * 0.18, 260, QColor(100, 30, 200, 130))   # deep purple
        self._orb(p, w * 0.75, h * 0.08, 200, QColor(200, 50, 120, 110))   # magenta
        self._orb(p, w * 0.88, h * 0.72, 230, QColor(200, 90, 20, 100))    # amber
        self._orb(p, w * 0.25, h * 0.85, 160, QColor(20, 100, 200, 85))    # blue
        self._orb(p, w * 0.55, h * 0.45, 150, QColor(80, 20, 160, 60))     # center purple

        # Dark frosted glass overlay on top of orbs
        p.setClipping(False)
        p.fillPath(clip, QColor(5, 3, 12, 175))

        # Outer border with slight purple tint
        p.setPen(QPen(QColor(160, 120, 255, 40), 1))
        p.drawPath(clip)

        # Sidebar frosted panel
        sb = QPainterPath()
        sb.addRoundedRect(0, 0, SIDEBAR_W, h, 20, 20)
        sb.addRect(SIDEBAR_W // 2, 0, SIDEBAR_W // 2, h)
        p.fillPath(sb, QColor(255, 255, 255, 5))

        # Sidebar separator line
        p.setPen(QPen(QColor(160, 120, 255, 18), 1))
        p.drawLine(SIDEBAR_W, 0, SIDEBAR_W, h)

        # Top edge highlight
        hi = QLinearGradient(0, 0, 0, 50)
        hi.setColorAt(0, QColor(255, 255, 255, 14))
        hi.setColorAt(1, QColor(255, 255, 255, 0))
        hi_path = QPainterPath()
        hi_path.addRoundedRect(0, 0, w, 50, 20, 20)
        p.fillPath(hi_path, hi)

    def closeEvent(self, e):
        if self._voice:
            self._voice.stop()
            self._voice.wait(2000)
        e.accept()


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    island = FloatingIsland()
    window = MainWindow()
    voice  = VoiceThread()
    window.set_voice(voice)

    voice.status.connect(island.set_status)
    voice.status.connect(window.on_status)
    voice.woke.connect(lambda s: island.set_status(f"Recording… {s:.0%}"))
    voice.heard.connect(island.set_heard)
    voice.heard.connect(window.on_heard)
    voice.token.connect(window.on_token)
    voice.done.connect(window.on_done)
    voice.err.connect(island.set_status)
    voice.err.connect(window.on_error)

    island.show()
    window.show()
    voice.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
