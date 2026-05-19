#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JARVIS — Voice Activation Terminal Demo                                    ║
║  Step 1 of the Jarvis macOS AI agent build                                  ║
╚══════════════════════════════════════════════════════════════════════════════╝

WHAT THIS DOES
  1. Calibrates mic sensitivity at startup (1-second ambient noise reading)
  2. Listens continuously for the wake word "hey jarvis"
  3. Once detected, shows a live ● REC spinner while recording your command
  4. Runs your speech through Whisper (local AI) and prints the transcript
  5. Say "thank you jarvis" to exit, or press Ctrl+C

HOW TO RUN
  python voice_demo.py

CONFIG  (edit .env to change these)
  JARVIS_WAKE_WORD   which word triggers listening:
                       hey_jarvis | alexa | hey_mycroft | ok_nabu
                       default: hey_jarvis

  WHISPER_MODEL      transcription accuracy vs speed:
                       tiny   → fastest  (~75 MB)
                       base   → balanced (~145 MB)  ← default
                       small  → better   (~460 MB)
                       medium → best     (~1.5 GB)

  WAKE_THRESHOLD     wake word sensitivity (0.0–1.0):
                       lower  = triggers more easily (more false positives)
                       higher = needs clearer speech (may miss quiet voices)
                       default: 0.5

HOW THE CODE WORKS
  Single audio stream, callback-based (not blocking reads):
    - macOS only allows one input stream at a time, so we use one stream
      with a state machine inside the callback instead of opening/closing streams
    - "idle" state: every 80ms chunk is fed to openWakeWord for wake detection
    - "recording" state: chunks accumulate; silence ends the recording
    - Completed recordings go into msg_queue for the main thread to transcribe

  Silence detection:
    - At startup, we measure 1s of ambient noise and auto-set the threshold
    - If your room noise is 0.02 RMS, threshold becomes ~0.05 (2.5× ambient)
    - Without this, a fixed threshold like 0.015 would never detect silence
      in a typical room, so recording would run until the 15-second hard cap

EXIT
  - Say "thank you jarvis" (punctuation-safe — Whisper's commas are stripped)
  - Ctrl+C
"""

import os
import re
import sys
import time
import wave
import queue
import signal
import tempfile
import threading

import numpy as np
import sounddevice as sd
import whisper
import openwakeword
from openwakeword.model import Model
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

WAKE_WORD      = os.getenv("JARVIS_WAKE_WORD", "hey_jarvis")
WHISPER_MODEL  = os.getenv("WHISPER_MODEL", "base")
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))

SAMPLE_RATE    = 16000   # Hz — must match openWakeWord's requirement
CHUNK_SIZE     = 1280    # samples per chunk = 80ms @ 16 kHz (openWakeWord requirement)
SILENCE_SEC    = 1.5     # seconds of quiet that ends a recording
MIN_SPEECH_SEC = 0.3     # recordings shorter than this are discarded
MAX_RECORD_SEC = 15      # hard cap: stop recording even if silence never detected

EXIT_PHRASE    = "thank you jarvis"

AVAILABLE_WORDS = ["hey_jarvis", "alexa", "hey_mycroft", "ok_nabu"]

# Derived counts (computed once at module load)
_SILENCE_CHUNKS = int(SILENCE_SEC * SAMPLE_RATE / CHUNK_SIZE)
_MAX_CHUNKS     = int(MAX_RECORD_SEC * SAMPLE_RATE / CHUNK_SIZE)

# ══════════════════════════════════════════════════════════════════════════════
#  TERMINAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"

def style(text, *codes):
    return "".join(codes) + text + RESET

def normalize(text: str) -> str:
    """
    Strip punctuation and lowercase so 'Thank you, Jarvis.' matches
    'thank you jarvis'. Whisper almost always adds commas / periods.
    """
    return re.sub(r"[^a-z\s]", "", text.lower()).strip()

def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2) + 1e-9))

# ══════════════════════════════════════════════════════════════════════════════
#  MIC CALIBRATION
#  Measures ambient noise for 1 second and returns a silence threshold that
#  is 2.5× the room's background RMS — so real silence is always detectable.
# ══════════════════════════════════════════════════════════════════════════════

def calibrate_silence() -> float:
    """
    Measure ambient noise for ~1 second using the callback API (never blocks).
    Returns a silence threshold = 2.5 × ambient RMS, minimum 0.02.
    """
    print(style("  Calibrating mic — stay quiet for 1 second…", DIM), flush=True)

    levels   = []
    done     = threading.Event()
    n_chunks = max(1, int(SAMPLE_RATE / CHUNK_SIZE))  # ~12 chunks ≈ 1 second

    def _cal_cb(indata, _f, _t, _s):
        levels.append(rms(indata.flatten()))
        if len(levels) >= n_chunks:
            done.set()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=CHUNK_SIZE, callback=_cal_cb):
        done.wait(timeout=5.0)  # give up after 5 s if mic never fires

    ambient   = float(np.mean(levels)) if levels else 0.03
    threshold = max(0.02, ambient * 2.5)
    print(style(f"  done — ambient={ambient:.4f}  →  threshold={threshold:.4f}", DIM))
    return threshold

# ══════════════════════════════════════════════════════════════════════════════
#  SHARED STATE  (audio callback ↔ main thread)
# ══════════════════════════════════════════════════════════════════════════════

#  ("wake",  score)          — wake word fired, confidence score 0–1
#  ("audio", array, secs)    — recording done, raw int16 audio, duration in seconds
msg_queue  = queue.Queue()
stop_event = threading.Event()

# ── Audio callback state machine ──────────────────────────────────────────────
# All fields are read/written ONLY inside audio_callback (callback thread).
_cb = {
    "mode":        "idle",   # "idle" | "recording"
    "chunks":      [],       # audio chunks accumulated while recording
    "silence_cnt": 0,        # consecutive quiet chunks
    "last_trig":   0.0,      # timestamp of last wake trigger (cooldown)
    "rec_start":   0.0,      # timestamp recording began
    "silence_thr": 0.03,     # set by calibrate_silence() before stream opens
    "oww":         None,     # openWakeWord model (set in main before stream opens)
}

# ══════════════════════════════════════════════════════════════════════════════
#  AUDIO CALLBACK  — called by sounddevice every 80 ms on its own thread
# ══════════════════════════════════════════════════════════════════════════════

def audio_callback(indata, _frames, _time_info, _status):
    """
    Core of the pipeline. Keep this fast — no Whisper, no file I/O, no print.
    Use msg_queue to hand work to the main thread.

    State machine:
      idle      → run openWakeWord on each chunk
                  if score ≥ threshold: switch to "recording"
      recording → accumulate chunks
                  if silence_cnt ≥ limit OR chunk count ≥ max: send audio to main
    """
    if stop_event.is_set():
        return

    chunk = indata.flatten().copy()
    level = rms(chunk)

    if _cb["mode"] == "idle":
        preds = _cb["oww"].predict(chunk)
        score = preds.get(WAKE_WORD, 0.0)
        now   = time.time()
        if score >= WAKE_THRESHOLD and (now - _cb["last_trig"]) > 1.0:
            _cb.update(mode="recording", chunks=[], silence_cnt=0,
                       last_trig=now, rec_start=now)
            msg_queue.put(("wake", score))

    else:  # recording
        _cb["chunks"].append(chunk)
        if level <= _cb["silence_thr"]:
            _cb["silence_cnt"] += 1
        else:
            _cb["silence_cnt"] = 0

        if _cb["silence_cnt"] >= _SILENCE_CHUNKS or len(_cb["chunks"]) >= _MAX_CHUNKS:
            audio    = np.concatenate(_cb["chunks"])
            duration = time.time() - _cb["rec_start"]
            _cb["mode"]   = "idle"
            _cb["chunks"] = []
            msg_queue.put(("audio", audio, duration))

# ══════════════════════════════════════════════════════════════════════════════
#  RECORDING SPINNER  — runs on its own daemon thread while recording
# ══════════════════════════════════════════════════════════════════════════════

def recording_spinner(stop_spin: threading.Event):
    """Print a live spinner + elapsed time while recording is active."""
    frames = ["◐", "◓", "◑", "◒"]
    start  = time.time()
    i = 0
    while not stop_spin.is_set():
        elapsed = time.time() - start
        print(f"\r  {frames[i % 4]} Recording…  {elapsed:.1f}s", end="", flush=True)
        i += 1
        time.sleep(0.2)
    print("\r" + " " * 30 + "\r", end="", flush=True)  # clear spinner line

# ══════════════════════════════════════════════════════════════════════════════
#  TRANSCRIBE
# ══════════════════════════════════════════════════════════════════════════════

def transcribe(audio: np.ndarray, model) -> str:
    """
    Convert raw int16 audio → text via Whisper.
    Writes a temp WAV (Whisper needs a file), transcribes, then deletes it.
    Returns empty string on failure.
    """
    if len(audio) < SAMPLE_RATE * MIN_SPEECH_SEC:
        return ""

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name

    try:
        with wave.open(tmp, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())
        result = model.transcribe(tmp, language="en", fp16=False)
        return result["text"].strip()
    except Exception as e:
        print(style(f"\n  [Whisper error] {e}", RED))
        return ""
    finally:
        os.unlink(tmp)

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if WAKE_WORD not in AVAILABLE_WORDS:
        print(style(f"\n  '{WAKE_WORD}' is not supported. Options: {', '.join(AVAILABLE_WORDS)}\n", YELLOW))
        sys.exit(1)

    word_display = WAKE_WORD.replace("_", " ").upper()

    # ── Header ────────────────────────────────────────────────────────────────
    print(style("\n  ◉  JARVIS — Voice Demo", BOLD, CYAN))
    print(style("  " + "─" * 44, DIM))
    print(f"  Wake word  : {style(word_display, BOLD)}")
    print(f"  Whisper    : {style(WHISPER_MODEL, BOLD)}")
    print(f"  Threshold  : {style(str(WAKE_THRESHOLD), BOLD)}")
    print(f"  Exit phrase: {style(EXIT_PHRASE.upper(), BOLD)}")
    print(style("  " + "─" * 44, DIM) + "\n")

    # ── Step 1: Calibrate mic ─────────────────────────────────────────────────
    # Must happen before the main stream opens (can't have two streams open)
    silence_thr = calibrate_silence()
    _cb["silence_thr"] = silence_thr

    # ── Step 2: Load Whisper ──────────────────────────────────────────────────
    print(style(f"  Loading Whisper [{WHISPER_MODEL}]…", DIM))
    stt_model = whisper.load_model(WHISPER_MODEL)
    print(style("  Whisper ready.\n", GREEN))

    # ── Step 3: Load openWakeWord ─────────────────────────────────────────────
    print(style("  Checking wake word models…", DIM))
    openwakeword.utils.download_models()
    print(style(f"  Loading [{WAKE_WORD}]…", DIM))
    _cb["oww"] = Model(wakeword_models=[WAKE_WORD], inference_framework="onnx")
    print(style("  Detector ready.\n", GREEN))

    # ── Ctrl+C handler ────────────────────────────────────────────────────────
    def _sigint(_s, _f):
        print(style("\n\n  Interrupt — shutting down…", DIM))
        stop_event.set()
    signal.signal(signal.SIGINT, _sigint)

    # ── Ready ─────────────────────────────────────────────────────────────────
    print(style(f'  Say "{word_display}" to activate', BOLD))
    print(style(f'  Say "{EXIT_PHRASE.upper()}" to quit\n', DIM))

    spin_stop: threading.Event | None = None
    spin_thread: threading.Thread | None = None

    # ── Open audio stream (callback-based, non-blocking) ──────────────────────
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=CHUNK_SIZE,
        callback=audio_callback,
    ):
        while not stop_event.is_set():
            try:
                msg = msg_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            kind = msg[0]

            # ── Wake word fired ───────────────────────────────────────────────
            if kind == "wake":
                _, score = msg
                ts = time.strftime("%H:%M:%S")
                print(style(f"  [{ts}] ", DIM)
                      + style("● REC", GREEN, BOLD)
                      + style(f"  confidence {score:.0%}", DIM))

                # Start spinner on its own thread so it can run while
                # the main thread waits for the next queue message
                spin_stop   = threading.Event()
                spin_thread = threading.Thread(
                    target=recording_spinner, args=(spin_stop,), daemon=True
                )
                spin_thread.start()

            # ── Recording finished ────────────────────────────────────────────
            elif kind == "audio":
                _, audio, duration = msg

                # Stop spinner
                if spin_stop:
                    spin_stop.set()
                if spin_thread:
                    spin_thread.join(timeout=0.5)

                if len(audio) < SAMPLE_RATE * MIN_SPEECH_SEC:
                    print(style("  (too short — try again)\n", DIM))
                    print(style(f'  Waiting for "{word_display}"…\n', DIM))
                    continue

                print(style(f"  Captured {duration:.1f}s — transcribing…", DIM))
                text = transcribe(audio, stt_model)

                if not text:
                    print(style("  (nothing transcribed — try again)\n", YELLOW))
                    print(style(f'  Waiting for "{word_display}"…\n', DIM))
                    continue

                # ── Print what you said ───────────────────────────────────────
                print(style("\n  ┌─────────────────────────────────────────────", CYAN))
                print(style("  │  You said: ", CYAN) + style(f'"{text}"', BOLD))
                print(style("  └─────────────────────────────────────────────\n", CYAN))

                # ── Check exit phrase (punctuation-stripped comparison) ────────
                if EXIT_PHRASE in normalize(text):
                    print(style("  Goodbye!\n", GREEN, BOLD))
                    stop_event.set()
                    break

                print(style(f'  Waiting for "{word_display}"…\n', DIM))

    print(style("  Jarvis offline.\n", DIM))


if __name__ == "__main__":
    main()
