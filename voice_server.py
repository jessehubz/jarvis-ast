#!/usr/bin/env python3
"""
JARVIS Voice Server

  source .venv/bin/activate && python voice_server.py

React app connects to ws://localhost:8765 automatically.
"""

import asyncio
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import wave

import numpy as np
import sounddevice as sd
import whisper
import ollama
import openwakeword
import websockets
from openwakeword.model import Model
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

WHISPER_MODEL  = os.getenv("WHISPER_MODEL", "base")
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.35"))
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "llama3.2")
WS_PORT        = int(os.getenv("JARVIS_WS_PORT", "8765"))

_raw       = os.getenv("JARVIS_WAKE_WORDS", "hey_jarvis")
WAKE_WORDS = [w.strip() for w in _raw.split(",") if w.strip()]
AVAILABLE  = ["hey_jarvis", "alexa", "hey_mycroft", "ok_nabu"]

SAMPLE_RATE    = 16000
CHUNK_SIZE     = 1280        # 80 ms per chunk
SILENCE_SEC    = 1.0         # seconds of quiet that ends a recording
MIN_SPEECH_SEC = 0.3         # shorter recordings are discarded
MAX_RECORD_SEC = 15

_SILENCE_CHUNKS = int(SILENCE_SEC * SAMPLE_RATE / CHUNK_SIZE)
_MAX_CHUNKS     = int(MAX_RECORD_SEC * SAMPLE_RATE / CHUNK_SIZE)

SYSTEM_PROMPT = (
    "You are Jarvis, a concise personal AI assistant running on macOS. "
    "Keep answers short and conversational. Never mention being an AI."
)

# ── WebSocket broadcast ───────────────────────────────────────────────────────

_clients:    set                       = set()
_event_loop: asyncio.AbstractEventLoop = None


def emit(ev_type: str, **kwargs):
    if _event_loop is None:
        return
    msg = json.dumps({"type": ev_type, **kwargs})
    asyncio.run_coroutine_threadsafe(_broadcast(msg), _event_loop)


async def _broadcast(msg: str):
    for client in list(_clients):
        try:
            await client.send(msg)
        except Exception:
            _clients.discard(client)

# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    return re.sub(r"[^a-z\s]", "", text.lower()).strip()


def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2) + 1e-9))


def is_exit(text: str) -> bool:
    n = normalize(text)
    return "thank you jarvis" in n or "thanks jarvis" in n or "goodbye jarvis" in n


def strip_wake_prefix(text: str) -> str:
    for prefix in ("hey, jarvis,", "hey, jarvis", "hey jarvis,", "hey jarvis"):
        if text.lower().startswith(prefix):
            return text[len(prefix):].lstrip(" ,").strip()
    return text

# ── Shared audio state ────────────────────────────────────────────────────────

msg_queue    = queue.Queue()
inject_queue = queue.Queue()
stop_event   = threading.Event()

_state = {
    "mode":            "idle",   # "idle" | "recording"
    "chunks":          [],
    "silence_cnt":     0,
    "last_trig":       0.0,
    "rec_start":       0.0,
    "silence_thr":     0.03,     # calibrated at startup
    "oww":             None,
    "in_conversation": False,    # True after "hey jarvis", False after "thank you"
    "busy":            False,    # True while transcribing / generating (mic paused)
    "dbg_count":       0,
}

# ── Audio callback ────────────────────────────────────────────────────────────

def audio_callback(indata, _frames, _time_info, _status):
    if stop_event.is_set():
        return
    try:
        chunk = indata.flatten().copy()
        level = rms(chunk)

        if _state["mode"] == "idle":
            if _state["oww"] is None:
                return

            # CRITICAL: always call predict() on every chunk so OWW's internal
            # sliding-window buffer stays current.  Skipping chunks while in
            # conversation mode causes the buffer to go stale and the model
            # needs many seconds to "warm up" again — making hey-jarvis seem broken.
            preds = _state["oww"].predict(chunk)

            if _state["dbg_count"] < 3:
                _state["dbg_count"] += 1
                print(f"  [dbg] OWW keys={list(preds.keys())} "
                      f"vals={[round(float(v),4) for v in preds.values()]}", flush=True)

            score = float(max(preds.values(), default=0.0))
            now   = time.time()

            # Wake word fires in ALL modes — works both for the first activation
            # and for saying "hey jarvis" again mid-conversation.
            if score >= WAKE_THRESHOLD and (now - _state["last_trig"]) > 1.0:
                _state.update(mode="recording", chunks=[], silence_cnt=0,
                              last_trig=now, rec_start=now)
                msg_queue.put(("wake", score))
                return

            # In conversation mode: also start recording when speech is detected
            # (so the user doesn't have to say "hey jarvis" every turn).
            # The busy flag keeps the mic paused while Jarvis is generating.
            if _state["in_conversation"] and not _state["busy"]:
                speech_thr = _state["silence_thr"] * 4.0
                if level > speech_thr:
                    _state.update(mode="recording", chunks=[chunk],
                                  silence_cnt=0, rec_start=time.time())

        else:  # recording mode
            _state["chunks"].append(chunk)
            _state["silence_cnt"] = (_state["silence_cnt"] + 1
                                     if level <= _state["silence_thr"] else 0)
            done = (
                _state["silence_cnt"] >= _SILENCE_CHUNKS or
                len(_state["chunks"]) >= _MAX_CHUNKS
            )
            if done:
                audio    = np.concatenate(_state["chunks"])
                duration = time.time() - _state["rec_start"]
                _state["mode"]   = "idle"
                _state["chunks"] = []
                msg_queue.put(("audio", audio, duration))

    except Exception as e:
        print(f"[audio_callback error] {e}", file=sys.stderr, flush=True)

# ── Calibration ───────────────────────────────────────────────────────────────

def calibrate() -> float:
    print("  Calibrating mic — stay quiet 1 second…", flush=True)
    emit("status", text="Calibrating mic…")
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
    thr     = max(0.02, ambient * 2.5)
    print(f"  ambient={ambient:.4f}  silence_thr={thr:.4f}  "
          f"speech_onset_thr={thr*4:.4f}", flush=True)
    return thr

# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe(audio: np.ndarray, stt) -> str:
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
        return stt.transcribe(
            tmp,
            language="en",
            fp16=False,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            temperature=0,
        )["text"].strip()
    except Exception as e:
        emit("error", text=f"Whisper error: {e}")
        return ""
    finally:
        os.unlink(tmp)

# ── LLM ──────────────────────────────────────────────────────────────────────

def ask_jarvis(text: str, history: list) -> str:
    history.append({"role": "user", "content": text})
    emit("status", text="Thinking…")
    full = ""
    try:
        for chunk in ollama.chat(model=OLLAMA_MODEL, messages=history, stream=True):
            t = chunk["message"]["content"]
            emit("token", text=t)
            full += t
    except ollama.ResponseError as e:
        emit("error", text=f"Ollama error: {e}")
    except Exception:
        emit("error", text="Ollama not running — start it with: ollama serve")
    if full:
        history.append({"role": "assistant", "content": full})
    emit("done")
    return full


def handle_text(text: str, history: list):
    emit("heard", text=text)
    clean = normalize(text)
    is_greeting = (
        any(g in clean for g in ("good morning", "good afternoon", "good evening", "good night"))
        and "jarvis" in clean
    )
    if is_greeting:
        h   = time.localtime().tm_hour
        tod = ("morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17
               else "evening" if 17 <= h < 22 else "night")
        ask_jarvis(
            f"The user greeted you with '{text}'. It is {tod}. Brief warm greeting.",
            history
        )
    else:
        ask_jarvis(text, history)

# ── Voice pipeline ────────────────────────────────────────────────────────────

def _run_voice():
    try:
        _state["silence_thr"] = calibrate()

        print("  Loading Whisper…", flush=True)
        emit("status", text="Loading Whisper…")
        stt = whisper.load_model(WHISPER_MODEL)
        print("  Whisper ready.", flush=True)

        print("  Loading wake word model…", flush=True)
        emit("status", text="Loading wake word model…")
        openwakeword.utils.download_models()
        _state["oww"] = Model(wakeword_models=WAKE_WORDS, inference_framework="onnx")
        print(f"  OWW ready. Prediction keys: {list(_state['oww'].prediction_buffer.keys())}",
              flush=True)

        emit("status", text="Checking Ollama…")
        try:
            available = [m.model for m in ollama.list().models]
            if not any(OLLAMA_MODEL in m for m in available):
                emit("warn", text=f"Model '{OLLAMA_MODEL}' not found — run: ollama pull {OLLAMA_MODEL}")
                print(f"  ⚠  '{OLLAMA_MODEL}' not found.", flush=True)
            else:
                print(f"  Ollama ready ({OLLAMA_MODEL}).", flush=True)
        except Exception:
            emit("warn", text="Ollama not running — run: ollama serve")
            print("  ⚠  Ollama not running.", flush=True)

        emit("status", text="Listening…")
        emit("ready")
        print(f"\n  ✓ Say 'hey jarvis' to start  |  'thank you jarvis' to stop talking\n",
              flush=True)

        history = [{"role": "system", "content": SYSTEM_PROMPT}]

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=CHUNK_SIZE, callback=audio_callback):
            while not stop_event.is_set():

                # ── Typed text from UI ────────────────────────────────────────
                try:
                    text = inject_queue.get_nowait()
                    _state["busy"] = True
                    try:
                        if is_exit(text):
                            ask_jarvis("The user is saying goodbye. Brief farewell.", history)
                            _state["in_conversation"] = False
                            emit("status", text="Listening…")
                        else:
                            handle_text(text, history)
                            if _state["in_conversation"]:
                                emit("status", text="Listening…")
                    finally:
                        _state["busy"] = False
                    continue
                except queue.Empty:
                    pass

                # ── Voice events ──────────────────────────────────────────────
                try:
                    msg = msg_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if msg[0] == "wake":
                    _, score = msg
                    _state["in_conversation"] = True
                    print(f"  ● Wake word (score={score:.2f})", flush=True)
                    emit("woke", score=score)
                    emit("status", text="Listening…")

                elif msg[0] == "audio":
                    _, audio, _dur = msg

                    # Pause the mic while we transcribe + respond.
                    # audio_callback returns immediately when busy=True,
                    # so no new recording starts until we're done.
                    _state["busy"] = True
                    try:
                        if len(audio) < SAMPLE_RATE * MIN_SPEECH_SEC:
                            continue

                        emit("status", text="Transcribing…")
                        text = transcribe(audio, stt)

                        if not text:
                            continue

                        # Remove accidental leading "hey jarvis" in the recording
                        text = strip_wake_prefix(text)
                        if not text:
                            continue

                        print(f"  You: {text}", flush=True)

                        if is_exit(text):
                            ask_jarvis("The user is saying goodbye. Brief farewell.", history)
                            _state["in_conversation"] = False
                            emit("status", text="Listening…")
                            print("  ○ Conversation ended. Say 'hey jarvis' to start again.\n",
                                  flush=True)
                            continue

                        handle_text(text, history)
                        if _state["in_conversation"]:
                            emit("status", text="Listening…")

                    finally:
                        # Always unlock — audio_callback resumes VAD + OWW listening
                        _state["busy"] = False

    except Exception as e:
        import traceback
        print(f"\n  ✗ Voice pipeline error: {e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        emit("error", text=str(e))


# ── WebSocket server ──────────────────────────────────────────────────────────

async def _ws_handler(websocket):
    _clients.add(websocket)
    print(f"  [ws] connected ({len(_clients)} total)", flush=True)
    try:
        async for message in websocket:
            try:
                cmd = json.loads(message)
                if cmd.get("type") == "text":
                    inject_queue.put(cmd["text"])
                elif cmd.get("type") == "stop":
                    stop_event.set()
            except Exception:
                pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _clients.discard(websocket)
        print(f"  [ws] disconnected ({len(_clients)} remaining)", flush=True)


async def _ws_main():
    global _event_loop
    _event_loop = asyncio.get_event_loop()
    threading.Thread(target=_run_voice, daemon=True).start()
    print(f"  WebSocket on ws://localhost:{WS_PORT}", flush=True)
    async with websockets.serve(_ws_handler, "localhost", WS_PORT):
        await asyncio.Future()

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    bad = [w for w in WAKE_WORDS if w not in AVAILABLE]
    if bad:
        print(f"Unknown wake words: {bad}. Available: {AVAILABLE}")
        sys.exit(1)

    print("\n  ◉  JARVIS Voice Server")
    print("  " + "─" * 44)
    print(f"  Wake words : {WAKE_WORDS}")
    print(f"  LLM model  : {OLLAMA_MODEL}")
    print(f"  Whisper    : {WHISPER_MODEL}")
    print(f"  Threshold  : {WAKE_THRESHOLD}")
    print("  " + "─" * 44 + "\n")

    try:
        asyncio.run(_ws_main())
    except KeyboardInterrupt:
        stop_event.set()
        print("\n  Jarvis offline.\n")


if __name__ == "__main__":
    main()
