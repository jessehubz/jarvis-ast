#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JARVIS — Voice Demo                                                         ║
╚══════════════════════════════════════════════════════════════════════════════╝

WHAT THIS DOES
  1. Calibrates mic sensitivity at startup (1-second ambient noise reading)
  2. Listens continuously for the wake word "hey jarvis"
  3. Records your command/question until you go quiet
  4. Transcribes speech → text with Whisper (local, free)
  5. Sends the text to Ollama (local AI model, free, unlimited)
  6. Streams Jarvis's response to the terminal word by word
  7. Remembers the conversation so follow-up questions work
  8. Say "thank you jarvis" to exit

HOW TO RUN
  First-time setup:
    brew install ollama
    ollama pull llama3.2          # ~2 GB, good balance of speed and quality
    ollama serve                  # start the Ollama server (keep this running)

  Then in a new terminal:
    source .venv/bin/activate
    python voice_demo.py

CONFIG  (edit .env to change these)
  JARVIS_WAKE_WORD   hey_jarvis | alexa | hey_mycroft | ok_nabu
                     default: hey_jarvis

  OLLAMA_MODEL       any model you have pulled via "ollama pull <name>"
                     default: llama3.2
                     fast/small options: llama3.2:1b, mistral, phi3

  WHISPER_MODEL      tiny | base | small | medium
                     default: base

  WAKE_THRESHOLD     wake word sensitivity 0.0–1.0
                     default: 0.5

HOW IT WORKS
  One audio stream runs the entire time using a callback (never blocks):
    - "idle" mode:     every 80ms chunk fed to openWakeWord for wake detection
    - "recording" mode: chunks accumulate until silence or 15s cap
    - finished recording goes into a queue for the main thread

  Main thread loop:
    wake message  → print ● REC, start spinner thread
    audio message → stop spinner, transcribe (Whisper), ask Ollama, print response

  Conversation history is kept in memory so Jarvis understands follow-ups like
  "tell me more" or "what about the second one" without re-explaining context.

EXIT
  Say "thank you jarvis"  or  press Ctrl+C
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
import ollama
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
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "llama3.2")

SAMPLE_RATE    = 16000
CHUNK_SIZE     = 1280    # 80ms @ 16kHz — openWakeWord's required frame size
SILENCE_SEC    = 1.5     # seconds of quiet that ends a recording
MIN_SPEECH_SEC = 0.3     # discard recordings shorter than this
MAX_RECORD_SEC = 15      # hard cap if silence never detected

EXIT_PHRASE    = "thank you jarvis"

AVAILABLE_WORDS = ["hey_jarvis", "alexa", "hey_mycroft", "ok_nabu"]

_SILENCE_CHUNKS = int(SILENCE_SEC * SAMPLE_RATE / CHUNK_SIZE)
_MAX_CHUNKS     = int(MAX_RECORD_SEC * SAMPLE_RATE / CHUNK_SIZE)

# Jarvis's personality — prepended to every conversation
SYSTEM_PROMPT = (
    "You are Jarvis, a concise and helpful personal AI assistant running locally on macOS. "
    "Keep answers brief and conversational unless the user asks for detail. "
    "Never say you're an AI or mention your model name."
)

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
    """Strip punctuation + lowercase. Lets 'Thank you, Jarvis.' match 'thank you jarvis'."""
    return re.sub(r"[^a-z\s]", "", text.lower()).strip()

def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2) + 1e-9))

# ══════════════════════════════════════════════════════════════════════════════
#  MIC CALIBRATION
#  Records 1s of ambient noise to set a silence threshold that works in
#  your actual room. Without this, a fixed threshold often misses silence
#  (room noise > threshold) so recording runs to the 15s hard cap.
# ══════════════════════════════════════════════════════════════════════════════

def calibrate_silence() -> float:
    print(style("  Calibrating mic — stay quiet for 1 second…", DIM), flush=True)

    levels   = []
    done     = threading.Event()
    n_chunks = max(1, int(SAMPLE_RATE / CHUNK_SIZE))

    def _cb(indata, _f, _t, _s):
        levels.append(rms(indata.flatten()))
        if len(levels) >= n_chunks:
            done.set()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=CHUNK_SIZE, callback=_cb):
        done.wait(timeout=5.0)

    ambient   = float(np.mean(levels)) if levels else 0.03
    threshold = max(0.02, ambient * 2.5)
    print(style(f"  done — ambient={ambient:.4f}  →  threshold={threshold:.4f}", DIM))
    return threshold

# ══════════════════════════════════════════════════════════════════════════════
#  SHARED STATE  (audio callback <-> main thread)
# ══════════════════════════════════════════════════════════════════════════════

#  ("wake",  score)        — wake word fired
#  ("audio", array, secs)  — recording finished
msg_queue  = queue.Queue()
stop_event = threading.Event()

_cb = {
    "mode":        "idle",
    "chunks":      [],
    "silence_cnt": 0,
    "last_trig":   0.0,
    "rec_start":   0.0,
    "silence_thr": 0.03,   # overwritten by calibrate_silence()
    "oww":         None,   # openWakeWord model, set in main()
}

# ══════════════════════════════════════════════════════════════════════════════
#  AUDIO CALLBACK  — sounddevice calls this every 80ms on its own thread
# ══════════════════════════════════════════════════════════════════════════════

def audio_callback(indata, _frames, _time_info, _status):
    """
    State machine:
      idle      → detect wake word → switch to "recording"
      recording → accumulate chunks → send to main thread when done
    Keep this fast: no Whisper, no Ollama, no file I/O.
    """
    if stop_event.is_set():
        return

    chunk = indata.flatten().copy()
    level = rms(chunk)

    if _cb["mode"] == "idle":
        score = _cb["oww"].predict(chunk).get(WAKE_WORD, 0.0)
        now   = time.time()
        if score >= WAKE_THRESHOLD and (now - _cb["last_trig"]) > 1.0:
            _cb.update(mode="recording", chunks=[], silence_cnt=0,
                       last_trig=now, rec_start=now)
            msg_queue.put(("wake", score))
    else:
        _cb["chunks"].append(chunk)
        _cb["silence_cnt"] = _cb["silence_cnt"] + 1 if level <= _cb["silence_thr"] else 0
        if _cb["silence_cnt"] >= _SILENCE_CHUNKS or len(_cb["chunks"]) >= _MAX_CHUNKS:
            audio    = np.concatenate(_cb["chunks"])
            duration = time.time() - _cb["rec_start"]
            _cb["mode"]   = "idle"
            _cb["chunks"] = []
            msg_queue.put(("audio", audio, duration))

# ══════════════════════════════════════════════════════════════════════════════
#  RECORDING SPINNER  — shows elapsed time while mic is open
# ══════════════════════════════════════════════════════════════════════════════

def recording_spinner(stop_spin: threading.Event):
    frames = ["◐", "◓", "◑", "◒"]
    start  = time.time()
    i = 0
    while not stop_spin.is_set():
        print(f"\r  {frames[i % 4]} Recording…  {time.time() - start:.1f}s",
              end="", flush=True)
        i += 1
        time.sleep(0.2)
    print("\r" + " " * 32 + "\r", end="", flush=True)

# ══════════════════════════════════════════════════════════════════════════════
#  TRANSCRIBE  — Whisper converts raw audio -> text
# ══════════════════════════════════════════════════════════════════════════════

def transcribe(audio: np.ndarray, model) -> str:
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
        return model.transcribe(tmp, language="en", fp16=False)["text"].strip()
    except Exception as e:
        print(style(f"\n  [Whisper error] {e}", RED))
        return ""
    finally:
        os.unlink(tmp)

# ══════════════════════════════════════════════════════════════════════════════
#  ASK JARVIS  — sends text to Ollama, streams response to terminal
# ══════════════════════════════════════════════════════════════════════════════

def ask_jarvis(text: str, history: list) -> str:
    """
    Append the user's message to history, send the whole conversation to
    Ollama, and stream the response token-by-token to the terminal.
    Returns the full response text (also appended to history for context).
    """
    history.append({"role": "user", "content": text})

    print(style("\n  Jarvis: ", CYAN, BOLD), end="", flush=True)

    full_response = ""
    try:
        stream = ollama.chat(model=OLLAMA_MODEL, messages=history, stream=True)
        for chunk in stream:
            token = chunk["message"]["content"]
            print(token, end="", flush=True)
            full_response += token
        print("\n")
    except ollama.ResponseError as e:
        print(style(f"\n  [Ollama error] {e}", RED))
        print(style(f"  Make sure the model is pulled: ollama pull {OLLAMA_MODEL}", YELLOW))
    except Exception as e:
        print(style(f"\n  [Ollama error] {e}", RED))
        print(style("  Is Ollama running? Run: ollama serve", YELLOW))

    if full_response:
        history.append({"role": "assistant", "content": full_response})

    return full_response

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if WAKE_WORD not in AVAILABLE_WORDS:
        print(style(f"\n  '{WAKE_WORD}' not supported. Options: {', '.join(AVAILABLE_WORDS)}\n", YELLOW))
        sys.exit(1)

    word_display = WAKE_WORD.replace("_", " ").upper()

    # ── Header ────────────────────────────────────────────────────────────────
    print(style("\n  ◉  JARVIS", BOLD, CYAN))
    print(style("  " + "─" * 44, DIM))
    print(f"  Wake word : {style(word_display, BOLD)}")
    print(f"  AI model  : {style(OLLAMA_MODEL, BOLD)}  (via Ollama)")
    print(f"  Whisper   : {style(WHISPER_MODEL, BOLD)}")
    print(f"  Exit      : {style(EXIT_PHRASE.upper(), BOLD)}")
    print(style("  " + "─" * 44, DIM) + "\n")

    # ── Calibrate mic ─────────────────────────────────────────────────────────
    silence_thr = calibrate_silence()
    _cb["silence_thr"] = silence_thr

    # ── Load Whisper ──────────────────────────────────────────────────────────
    print(style(f"  Loading Whisper [{WHISPER_MODEL}]…", DIM))
    stt_model = whisper.load_model(WHISPER_MODEL)
    print(style("  Whisper ready.", GREEN))

    # ── Load openWakeWord ─────────────────────────────────────────────────────
    print(style("  Checking wake word models…", DIM))
    openwakeword.utils.download_models()
    _cb["oww"] = Model(wakeword_models=[WAKE_WORD], inference_framework="onnx")
    print(style("  Wake word detector ready.", GREEN))

    # ── Check Ollama is reachable (fast — no model loading) ──────────────────
    print(style(f"  Checking Ollama [{OLLAMA_MODEL}]…", DIM))
    try:
        available = [m.model for m in ollama.list().models]
        # Ollama stores models as "llama3.2:latest" so check with/without tag
        found = any(OLLAMA_MODEL in m for m in available)
        if not found:
            print(style(f"  Model '{OLLAMA_MODEL}' not found locally.", RED))
            print(style(f"  Run: ollama pull {OLLAMA_MODEL}", YELLOW))
            sys.exit(1)
        print(style("  Ollama ready.\n", GREEN))
    except Exception as e:
        print(style(f"  Cannot reach Ollama: {e}", RED))
        print(style("  Run: ollama serve  (in a separate terminal)\n", YELLOW))
        sys.exit(1)

    # ── Conversation history (keeps context across exchanges) ─────────────────
    history = [{"role": "system", "content": SYSTEM_PROMPT}]

    # ── Ctrl+C handler ────────────────────────────────────────────────────────
    def _sigint(_s, _f):
        print(style("\n\n  Shutting down…", DIM))
        stop_event.set()
    signal.signal(signal.SIGINT, _sigint)

    print(style(f'  Say "{word_display}" then ask anything', BOLD))
    print(style(f'  Say "{EXIT_PHRASE.upper()}" to quit\n', DIM))

    spin_stop:   threading.Event  | None = None
    spin_thread: threading.Thread | None = None

    # ── Main loop ─────────────────────────────────────────────────────────────
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=CHUNK_SIZE, callback=audio_callback):
        while not stop_event.is_set():
            try:
                msg = msg_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            kind = msg[0]

            if kind == "wake":
                _, score = msg
                ts = time.strftime("%H:%M:%S")
                print(style(f"  [{ts}] ", DIM)
                      + style("● REC", GREEN, BOLD)
                      + style(f"  confidence {score:.0%}", DIM))
                spin_stop   = threading.Event()
                spin_thread = threading.Thread(
                    target=recording_spinner, args=(spin_stop,), daemon=True)
                spin_thread.start()

            elif kind == "audio":
                _, audio, duration = msg

                if spin_stop:   spin_stop.set()
                if spin_thread: spin_thread.join(timeout=0.5)

                if len(audio) < SAMPLE_RATE * MIN_SPEECH_SEC:
                    print(style("  (too short — try again)\n", DIM))
                    continue

                # Transcribe
                print(style(f"  Captured {duration:.1f}s — transcribing…", DIM))
                text = transcribe(audio, stt_model)

                if not text:
                    print(style("  (nothing transcribed — try again)\n", YELLOW))
                    continue

                # Print what you said
                print(style("  You: ", DIM) + style(f'"{text}"', BOLD))

                # Check for exit phrase before sending to Ollama
                if EXIT_PHRASE in normalize(text):
                    ask_jarvis("The user is saying goodbye. Say a brief farewell.", history)
                    stop_event.set()
                    break

                # Ask Ollama and stream the response
                ask_jarvis(text, history)

                print(style(f'  Waiting for "{word_display}"…\n', DIM))

    print(style("  Jarvis offline.\n", DIM))


if __name__ == "__main__":
    main()
