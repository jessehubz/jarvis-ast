#!/usr/bin/env python3
"""
JARVIS Voice Server
====================
Run:  source .venv/bin/activate && python voice_server.py

State machine
─────────────
STANDBY     wake-word listening only (in_conversation=False)
ACTIVE      VAD-triggered recording (in_conversation=True, not busy/speaking)
RECORDING   collecting audio chunks
TRANSCRIBE  faster-whisper processing (busy=True)
THINKING    LLM streaming (busy=True)
SPEAKING    Swift TTS playing (tts_playing=True)
COOLDOWN    short silence after TTS before mic re-opens

Key invariants
──────────────
• OWW is fed on EVERY chunk in idle mode to keep its sliding window warm.
  Without this the model needs ~10s to "warm up" and wake-word fails.
• OWW scores are evaluated ONLY when tts_playing=False to prevent Jarvis's
  own speaker output from false-triggering wake-word detection (self-trigger bug).
• last_trig is reset to 0 when TTS ends so the user can say "hey jarvis"
  immediately after — not blocked by the WAKE_COOLDOWN from the previous trigger.
• VAD onset requires SPEECH_ONSET_CHUNKS consecutive above-threshold frames
  (~160 ms of sustained speech) to prevent noise pops starting a recording.
• tts_done is sent from Swift only when BOTH generation is confirmed finished
  ("done" event received) AND the speech queue has fully drained.  This prevents
  the mic from re-opening while Jarvis is still speaking.
"""

import asyncio
import json
import logging
import os
import queue
import re
import sys
import tempfile
import threading
import time
import uuid
import wave

import automation_engine
import task_planner

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
import ollama
import openwakeword
import websockets
from openwakeword.model import Model
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("jarvis")

# ── Config ────────────────────────────────────────────────────────────────────

# Set WHISPER_MODEL=tiny for ~4x faster transcription (slightly less accurate)
WHISPER_MODEL  = os.getenv("WHISPER_MODEL",    "tiny")
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.35"))
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL",     "llama3.2")
WS_PORT        = int(os.getenv("JARVIS_WS_PORT", "8765"))
# Set ENABLE_AUTOMATION=0 to disable desktop automation (faster if you don't need it)
ENABLE_AUTOMATION = os.getenv("ENABLE_AUTOMATION", "1") == "1"

_raw       = os.getenv("JARVIS_WAKE_WORDS", "hey_jarvis")
WAKE_WORDS = [w.strip() for w in _raw.split(",") if w.strip()]
AVAILABLE  = ["hey_jarvis", "alexa", "hey_mycroft", "ok_nabu"]

# Audio
SAMPLE_RATE         = 16000
CHUNK_SIZE          = 1280       # 80 ms per chunk
SILENCE_SEC         = 1.2        # silence duration that ends a recording
MIN_SPEECH_SEC      = 0.4        # shorter clips skipped before Whisper
MAX_RECORD_SEC      = 15
SPEECH_ONSET_CHUNKS = 2          # consecutive above-threshold chunks to start VAD rec (~160 ms)
WAKE_COOLDOWN       = 1.5        # min seconds between wake triggers

# Conversation
CONVERSATION_TIMEOUT = 90.0      # auto-exit conversation after this much silence
MAX_HISTORY_TURNS    = 8         # keep last N user+assistant pairs (prevents context bloat)

# TTS
TTS_COOLDOWN = 0.8  # seconds after TTS ends before VAD re-opens the mic.

# Watchdog
WATCHDOG_INTERVAL    = 5.0
BUSY_TIMEOUT         = 45.0
TTS_TIMEOUT          = 90.0
STREAM_RESTART_DELAY = 1.5

_SILENCE_CHUNKS = int(SILENCE_SEC * SAMPLE_RATE / CHUNK_SIZE)
_MAX_CHUNKS     = int(MAX_RECORD_SEC * SAMPLE_RATE / CHUNK_SIZE)

SYSTEM_PROMPT = (
    "You are Jarvis, a concise personal AI assistant running on macOS. "
    "Keep answers short and conversational. Never mention being an AI."
)

# Background-conversation filter: skip transcriptions that match these patterns
# when NOT in an active conversation session.
_BG_PATTERN = re.compile(
    r"\b(you know what i mean|did you see that|check this out|"
    r"have you heard|so basically|let me show you|"
    r"i was telling|she said|he said|they said)\b",
    re.IGNORECASE,
)

# Keyword pre-filter: only call the Ollama automation planner when the message
# contains at least one action verb.  Avoids an expensive LLM round-trip for
# the vast majority of conversational messages.
_AUTOMATION_GATE = re.compile(
    r"\b(open|close|launch|quit|start|type|write|click|press|search|find|"
    r"go to|navigate|browse|screenshot|take a screenshot|move|copy|delete|"
    r"rename|create|make|show|display|get|fetch|download|run|execute|"
    r"play|pause|stop|mute|volume|zoom|minimize|maximize|switch|focus|"
    r"activate|email|calendar|reminder|alarm|note|file|folder|app|window)\b",
    re.IGNORECASE,
)


def _is_possibly_automation(text: str) -> bool:
    return bool(_AUTOMATION_GATE.search(text))


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
    return (
        "thank you jarvis"  in n
        or "thanks jarvis"  in n
        or "goodbye jarvis" in n
        or "stop listening" in n
        or "go to sleep"    in n
        or "end session"    in n
        or n.strip()        == "stop"
    )


def strip_wake_prefix(text: str) -> str:
    for prefix in ("hey, jarvis,", "hey, jarvis", "hey jarvis,", "hey jarvis"):
        if text.lower().startswith(prefix):
            return text[len(prefix):].lstrip(" ,").strip()
    return text


def is_directed_at_jarvis(text: str, in_conversation: bool, since_last: float) -> bool:
    """
    Returns True if this transcription is likely directed at Jarvis.

    Rules (in order of precedence):
    1. Explicit "Jarvis" mention → always yes.
    2. In active conversation AND ≤30s since last interaction → yes (natural follow-up).
    3. In active conversation AND >30s → require ≥4 words (filter stale ambient speech).
    4. Not in conversation:
       - ≥3 words AND no background-conversation pattern → yes.
       - Otherwise → no.
    """
    n = normalize(text)
    if "jarvis" in n:
        return True
    words = text.split()
    if in_conversation:
        if since_last <= 30.0:
            return True
        if len(words) >= 4:
            return True
        log.debug("[INTENT] stale/short in-conversation — dropped: '%s'", text)
        return False
    # Not in conversation: stricter
    if len(words) < 3:
        log.debug("[INTENT] too short outside conversation — dropped: '%s'", text)
        return False
    if _BG_PATTERN.search(text):
        log.info("[INTENT] background pattern — dropped: '%s'", text)
        return False
    return True


def trim_history(history: list):
    """Keep the system prompt + the last MAX_HISTORY_TURNS user/assistant pairs."""
    max_len = 1 + MAX_HISTORY_TURNS * 2
    if len(history) > max_len:
        system = history[0]
        recent = history[-MAX_HISTORY_TURNS * 2:]
        history.clear()
        history.append(system)
        history.extend(recent)
        log.debug("[HISTORY] trimmed to %d messages", len(history))

# ── Shared state ──────────────────────────────────────────────────────────────

msg_queue     = queue.Queue()
stop_event    = threading.Event()
restart_event = threading.Event()

# Shared conversation history — access always serialized by _llm_lock
_history:  list           = []
# Only one LLM call at a time (voice or text); prevents overlapping responses
_llm_lock: threading.Lock = threading.Lock()

_state = {
    # Audio FSM
    "mode":             "idle",   # "idle" | "recording"
    "chunks":           [],
    "lookback":         [],       # rolling last-8-chunk buffer to capture speech onset
    "silence_cnt":      0,
    "onset_count":      0,        # VAD onset debounce
    "last_trig":        0.0,      # timestamp of last wake-word trigger
    "rec_start":        0.0,

    # Calibration
    "silence_thr":      0.03,

    # Subsystem handles
    "oww":              None,

    # Conversation
    "in_conversation":  False,
    "last_interaction": 0.0,

    # Pipeline locks
    "busy":             False,
    "busy_since":       0.0,
    "tts_playing":      False,
    "tts_since":        0.0,
    "cooldown_until":   0.0,      # VAD onset blocked until this timestamp (post-TTS)

    # Health
    "stream_errors":    0,
    "pipeline_runs":    0,
    "dbg_count":        0,
}

# ── Task state ────────────────────────────────────────────────────────────────

_task_state = {
    "active":  False,
    "task_id": "",
    "cancel":  threading.Event(),
}

# ── Task execution ────────────────────────────────────────────────────────────

def _run_task(task_id: str, plan: dict):
    """Execute a task plan. Runs in a daemon thread; does NOT hold _llm_lock."""
    steps = plan.get("steps", [])
    _task_state["active"]  = True
    _task_state["task_id"] = task_id
    _task_state["cancel"].clear()

    # Remember the user's active app and current space so we can restore after.
    original_app = automation_engine.get_frontmost_app()
    log.debug("[TASK] saving focus: '%s'", original_app)

    emit("task_start",
         task_id=task_id,
         task_name=plan.get("task_name", "Task"),
         steps=[{"id": s["id"], "name": s["name"]} for s in steps],
         estimated_seconds=plan.get("estimated_seconds", 0))

    # Switch to Desktop 2 so the task runs isolated from the user's workspace.
    switched_space = False
    ok_space, _ = automation_engine.switch_space(2)
    if ok_space:
        switched_space = True
        log.info("[TASK] switched to Desktop 2")
    else:
        log.warning("[TASK] could not switch to Desktop 2 — running in current space")

    success = True
    try:
        for step in steps:
            if _task_state["cancel"].is_set():
                break

            emit("task_step_start", task_id=task_id, step_id=step["id"])
            ok, msg = automation_engine.execute_step(step)

            if step.get("action") == "screenshot" and ok:
                emit("task_preview", task_id=task_id, image=msg)
                emit("task_step_done", task_id=task_id, step_id=step["id"],
                     message="Screenshot captured")
            elif ok:
                emit("task_step_done", task_id=task_id, step_id=step["id"],
                     message=msg or "Done")
            else:
                emit("task_step_error", task_id=task_id, step_id=step["id"],
                     message=msg or "Failed")
                success = False
                break
    finally:
        # Switch back to Desktop 1 and restore focus
        if switched_space:
            automation_engine.switch_space(1)
            log.info("[TASK] returned to Desktop 1")
        automation_engine.restore_focus(original_app)

    if _task_state["cancel"].is_set():
        emit("task_cancelled", task_id=task_id)
    elif success:
        emit("task_complete", task_id=task_id)
    else:
        emit("task_error", task_id=task_id)

    _task_state["active"]  = False
    _task_state["task_id"] = ""


def _try_automation(text: str, emit_heard_text: str | None = None) -> bool:
    """
    Detect and launch an automation task.  Must be called while holding _llm_lock.
    emit_heard_text: if not None, emit "heard" with this text before streaming tokens
                     (voice path); None means "heard" was already emitted (text path).
    Returns True if a task was started (caller should skip normal LLM call).
    """
    if not ENABLE_AUTOMATION:
        return False
    if _task_state["active"]:
        return False
    if not _is_possibly_automation(text):   # fast keyword gate — avoids Ollama call
        return False

    plan = task_planner.plan_automation(text)
    if not plan.get("is_automation"):
        return False

    _state["last_interaction"] = time.time()
    _state["in_conversation"]  = True

    if emit_heard_text is not None:
        emit("heard", text=emit_heard_text)

    verbal = plan.get("verbal_response", "On it.")
    emit("token", text=verbal)
    _state["tts_playing"] = True
    _state["tts_since"]   = time.time()
    emit("status", text="Speaking…")
    emit("done")

    task_id = uuid.uuid4().hex[:8]
    threading.Thread(
        target=_run_task, args=(task_id, plan),
        daemon=True, name="task-executor",
    ).start()
    return True


# ── Audio callback ────────────────────────────────────────────────────────────

def audio_callback(indata, _frames, _time_info, status):
    if stop_event.is_set():
        return

    if status:
        log.debug("[AUDIO] status=%s", status)
        if hasattr(status, "input_overflow") and status.input_overflow:
            _state["stream_errors"] += 1

    try:
        chunk = indata.flatten().copy()
        level = rms(chunk)

        if _state["mode"] == "idle":
            oww = _state["oww"]
            if oww is None:
                return

            # Maintain a rolling look-back buffer so we don't miss the first
            # syllables when VAD onset fires after SPEECH_ONSET_CHUNKS frames.
            lb = _state["lookback"]
            lb.append(chunk)
            if len(lb) > 8:
                lb.pop(0)

            # ALWAYS feed OWW to keep the sliding-window buffer warm.
            # Skipping even a few seconds makes the model need a multi-second
            # re-warm and "hey jarvis" appears broken.
            preds = oww.predict(chunk)

            if _state["dbg_count"] < 3:
                _state["dbg_count"] += 1
                log.debug("OWW keys=%s vals=%s",
                          list(preds.keys()),
                          [round(float(v), 4) for v in preds.values()])

            score = float(max(preds.values(), default=0.0))
            now   = time.time()

            # ── Wake-word check ────────────────────────────────────────────
            # Skip acting on the score while TTS is playing.
            # Jarvis's own speaker output can cross the wake threshold and
            # update last_trig, which then blocks the *real* "hey jarvis"
            # the user says right after TTS finishes (self-trigger bug).
            # We still fed OWW above so the buffer stays warm.
            if not _state["tts_playing"]:
                if score >= WAKE_THRESHOLD and (now - _state["last_trig"]) > WAKE_COOLDOWN:
                    log.info("[WAKE] score=%.3f", score)
                    _state.update(mode="recording", chunks=[chunk], silence_cnt=0,
                                  onset_count=0, last_trig=now, rec_start=now)
                    msg_queue.put(("wake", score))
                    return

            # ── Conversation VAD onset ─────────────────────────────────────
            # Only when: in active conversation, not busy, not TTS, past cooldown.
            # Require SPEECH_ONSET_CHUNKS consecutive above-threshold frames to
            # avoid noise pops starting a recording.
            if (_state["in_conversation"]
                    and not _state["busy"]
                    and not _state["tts_playing"]
                    and now >= _state["cooldown_until"]):
                speech_thr = _state["silence_thr"] * 3.0
                if level > speech_thr:
                    _state["onset_count"] += 1
                    if _state["onset_count"] >= SPEECH_ONSET_CHUNKS:
                        log.debug("[VAD] onset level=%.4f", level)
                        _state.update(mode="recording",
                                      chunks=list(_state["lookback"]) + [chunk],
                                      silence_cnt=0,
                                      onset_count=0,
                                      rec_start=time.time())
                else:
                    _state["onset_count"] = 0

        else:  # recording mode
            _state["chunks"].append(chunk)
            # Slightly relaxed silence threshold to avoid cutting off mid-word
            is_silent = level <= (_state["silence_thr"] * 1.5)
            _state["silence_cnt"] = _state["silence_cnt"] + 1 if is_silent else 0

            done = (
                _state["silence_cnt"] >= _SILENCE_CHUNKS
                or len(_state["chunks"]) >= _MAX_CHUNKS
            )
            if done:
                audio    = np.concatenate(_state["chunks"])
                duration = time.time() - _state["rec_start"]
                log.debug("[REC] %.2fs  %d samples", duration, len(audio))
                _state["mode"]        = "idle"
                _state["chunks"]      = []
                _state["lookback"]    = []   # reset so next utterance gets fresh look-back
                _state["onset_count"] = 0
                msg_queue.put(("audio", audio, duration))

    except Exception as exc:
        log.error("[AUDIO_CB] %s", exc, exc_info=True)

# ── Calibration ───────────────────────────────────────────────────────────────

def calibrate() -> float:
    log.info("Calibrating mic — stay quiet…")
    emit("status", text="Calibrating mic…")
    levels, done_ev = [], threading.Event()
    n = max(2, int(SAMPLE_RATE / CHUNK_SIZE))

    def _cb(indata, _f, _t, _s):
        levels.append(rms(indata.flatten()))
        if len(levels) >= n:
            done_ev.set()

    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=CHUNK_SIZE, callback=_cb):
            done_ev.wait(timeout=5.0)
    except Exception as exc:
        log.warning("[CAL] failed: %s — using default 0.03", exc)
        return 0.03

    if not levels:
        return 0.03

    ambient = float(np.mean(levels))
    thr     = max(0.02, ambient * 2.5)
    log.info("Calibration: ambient=%.4f  silence_thr=%.4f  speech_thr=%.4f",
             ambient, thr, thr * 4)
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
        segs, _info = stt.transcribe(
            tmp,
            language="en",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            beam_size=3,
            condition_on_previous_text=False,
        )
        result = " ".join(s.text.strip() for s in segs).strip()
        if result:
            log.info("[STT] '%s'", result)
        return result
    except Exception as exc:
        log.error("[STT] %s", exc, exc_info=True)
        emit("error", text=f"Transcription error: {exc}")
        return ""
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

# ── LLM ──────────────────────────────────────────────────────────────────────

def ask_jarvis(text: str, history: list) -> str:
    history.append({"role": "user", "content": text})
    emit("status", text="Thinking…")
    full = ""
    try:
        for chunk in ollama.chat(model=OLLAMA_MODEL, messages=history, stream=True):
            t = chunk["message"]["content"]
            if not t:   # last chunk often has content=None or content=""
                continue
            emit("token", text=t)
            full += t
    except ollama.ResponseError as exc:
        log.error("[LLM] ResponseError: %s", exc)
        emit("error", text=f"Ollama error: {exc}")
    except Exception as exc:
        log.error("[LLM] %s", exc, exc_info=True)
        emit("error", text="Ollama not running — start it with: ollama serve")

    if full:
        history.append({"role": "assistant", "content": full})
        trim_history(history)
        # Block VAD until Swift TTS queue drains; tts_done resets these.
        _state["tts_playing"] = True
        _state["tts_since"]   = time.time()
        emit("status", text="Speaking…")

    emit("done")
    return full


def handle_text(text: str, history: list, *, emit_heard: bool = True) -> str:
    if emit_heard:
        emit("heard", text=text)
    _state["last_interaction"] = time.time()
    clean = normalize(text)
    is_greeting = (
        any(g in clean for g in ("good morning", "good afternoon",
                                  "good evening", "good night"))
        and "jarvis" in clean
    )
    if is_greeting:
        h   = time.localtime().tm_hour
        tod = ("morning" if 5 <= h < 12 else
               "afternoon" if 12 <= h < 17 else
               "evening" if 17 <= h < 22 else "night")
        return ask_jarvis(
            f"The user greeted you with '{text}'. It is {tod}. Brief warm greeting.",
            history,
        )
    return ask_jarvis(text, history)

# ── Watchdog ──────────────────────────────────────────────────────────────────

def _watchdog():
    log.info("[WD] started")
    while not stop_event.is_set():
        time.sleep(WATCHDOG_INTERVAL)
        now = time.time()

        # Stuck busy
        if _state["busy"] and _state["busy_since"] > 0:
            elapsed = now - _state["busy_since"]
            if elapsed > BUSY_TIMEOUT:
                log.warning("[WD] busy stuck %.0fs — resetting", elapsed)
                _state["busy"]       = False
                _state["busy_since"] = 0.0
                if not _state["tts_playing"]:
                    emit("status", text="Listening…")

        # Stuck TTS
        if _state["tts_playing"] and _state["tts_since"] > 0:
            elapsed = now - _state["tts_since"]
            if elapsed > TTS_TIMEOUT:
                log.warning("[WD] tts stuck %.0fs — resetting", elapsed)
                _state["tts_playing"]  = False
                _state["tts_since"]    = 0.0
                _state["last_trig"]    = 0.0
                _state["cooldown_until"] = now + TTS_COOLDOWN
                emit("stop_tts")
                if not _state["busy"]:
                    emit("status", text="Listening…")

        # Conversation timeout
        if (_state["in_conversation"]
                and not _state["busy"]
                and not _state["tts_playing"]
                and _state["last_interaction"] > 0):
            idle = now - _state["last_interaction"]
            if idle > CONVERSATION_TIMEOUT:
                log.info("[WD] conversation timeout after %.0fs", idle)
                _state["in_conversation"] = False
                emit("status", text="Listening…")

        # Mode stuck in recording (shouldn't last >MAX_RECORD_SEC+2)
        if _state["mode"] == "recording" and _state["rec_start"] > 0:
            elapsed = now - _state["rec_start"]
            if elapsed > MAX_RECORD_SEC + 5:
                log.warning("[WD] recording stuck %.0fs — forcing idle", elapsed)
                _state["mode"]   = "idle"
                _state["chunks"] = []

        # Stream error threshold
        if _state["stream_errors"] >= 10:
            log.warning("[WD] %d stream errors — requesting restart",
                        _state["stream_errors"])
            _state["stream_errors"] = 0
            restart_event.set()

    log.info("[WD] stopped")

# ── Voice pipeline ────────────────────────────────────────────────────────────

def _run_voice():
    """Outer loop: loads models once, restarts only the stream on errors."""
    _state["pipeline_runs"] += 1
    log.info("[PIPELINE] run #%d", _state["pipeline_runs"])

    try:
        _state["silence_thr"] = calibrate()

        log.info("Loading faster-whisper (%s)…", WHISPER_MODEL)
        emit("status", text="Loading Whisper…")
        stt = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        log.info("Whisper ready")

        log.info("Loading wake-word model…")
        emit("status", text="Loading wake word model…")
        openwakeword.utils.download_models()
        _state["oww"] = Model(wakeword_models=WAKE_WORDS, inference_framework="onnx")
        log.info("OWW ready — keys: %s",
                 list(_state["oww"].prediction_buffer.keys()))

        emit("status", text="Checking Ollama…")
        try:
            available = [m.model for m in ollama.list().models]
            if not any(OLLAMA_MODEL in m for m in available):
                emit("warn", text=f"Model '{OLLAMA_MODEL}' not found — run: ollama pull {OLLAMA_MODEL}")
            else:
                log.info("Ollama ready (%s)", OLLAMA_MODEL)
        except Exception as exc:
            emit("warn", text="Ollama not running — run: ollama serve")
            log.warning("Ollama check failed: %s", exc)

        emit("status", text="Listening…")
        emit("ready")
        log.info("✓ Say 'hey jarvis' to start | 'thank you jarvis' to stop\n")

        _history.clear()
        _history.append({"role": "system", "content": SYSTEM_PROMPT})

        # Stream-session restart loop — only the stream restarts, not the models
        while not stop_event.is_set():
            _state["stream_errors"] = 0
            restart_event.clear()

            try:
                _stream_session(stt)
            except sd.PortAudioError as exc:
                log.error("[STREAM] PortAudioError: %s", exc)
                emit("status", text="Audio error — restarting…")
            except Exception as exc:
                log.error("[STREAM] %s", exc, exc_info=True)
                emit("status", text="Pipeline error — restarting…")

            if stop_event.is_set():
                break

            log.info("[PIPELINE] restarting stream in %.1fs…", STREAM_RESTART_DELAY)
            time.sleep(STREAM_RESTART_DELAY)
            try:
                _state["silence_thr"] = calibrate()
            except Exception as exc:
                log.warning("[CAL] recalibration failed: %s", exc)

    except Exception as exc:
        log.critical("[PIPELINE] fatal startup: %s", exc, exc_info=True)
        emit("error", text=str(exc))


def _text_worker(text: str):
    """
    Process typed text from the UI.
    Runs in its own daemon thread so the WebSocket handler (and voice loop)
    are never blocked by a slow LLM call.  Serialized with _voice_worker via
    _llm_lock so history is always accessed by exactly one thread at a time.
    """
    log.info("[UI] '%s'", text)
    if not _llm_lock.acquire(blocking=True, timeout=60):
        log.warning("[TEXT] lock timeout — dropping '%s'", text)
        emit("error", text="System busy — please try again")
        return
    _state["busy"]       = True
    _state["busy_since"] = time.time()
    try:
        if is_exit(text):
            ask_jarvis("The user is saying goodbye. Brief farewell.", _history)
            _state["in_conversation"] = False
            if not _state["tts_playing"]:
                emit("status", text="Listening…")
        elif _try_automation(text, emit_heard_text=None):
            # "heard" was already emitted by the WS handler; automation took over
            pass
        else:
            _state["in_conversation"] = True
            _state["last_interaction"] = time.time()
            # "heard" was already emitted by the WS handler so the user bubble
            # appears instantly — pass emit_heard=False to avoid a duplicate.
            handle_text(text, _history, emit_heard=False)
            if _state["in_conversation"] and not _state["tts_playing"]:
                emit("status", text="Listening…")
    finally:
        _state["busy"]       = False
        _state["busy_since"] = 0.0
        _llm_lock.release()


def _voice_worker(audio: np.ndarray, stt):
    """
    Transcribe + LLM for a recorded voice chunk.
    Runs in its own daemon thread; serialized via _llm_lock.
    """
    if not _llm_lock.acquire(blocking=True, timeout=60):
        log.warning("[VOICE] lock timeout — dropping audio")
        if _state["in_conversation"]:
            emit("status", text="Listening…")
        return
    _state["busy"]       = True
    _state["busy_since"] = time.time()
    try:
        if len(audio) < SAMPLE_RATE * MIN_SPEECH_SEC:
            log.debug("[AUDIO] too short — skipped")
            if _state["in_conversation"]:
                emit("status", text="Listening…")
            return

        emit("status", text="Transcribing…")
        text = transcribe(audio, stt)

        if not text:
            if _state["in_conversation"]:
                emit("status", text="Listening…")
            return

        text = strip_wake_prefix(text)
        if not text:
            if _state["in_conversation"]:
                emit("status", text="Listening…")
            return

        since = time.time() - _state["last_interaction"]
        if not is_directed_at_jarvis(text, _state["in_conversation"], since):
            log.info("[INTENT] dropped: '%s'", text)
            if _state["in_conversation"]:
                emit("status", text="Listening…")
            return

        log.info("[YOU] %s", text)

        if is_exit(text):
            ask_jarvis("The user is saying goodbye. Brief farewell.", _history)
            _state["in_conversation"] = False
            log.info("○ Conversation ended")
            if not _state["tts_playing"]:
                emit("status", text="Listening…")
            return

        if _try_automation(text, emit_heard_text=text):
            return

        handle_text(text, _history)
        if _state["in_conversation"] and not _state["tts_playing"]:
            emit("status", text="Listening…")

    finally:
        _state["busy"]       = False
        _state["busy_since"] = 0.0
        _llm_lock.release()


def _stream_session(stt):
    """
    Single audio-stream session.  The loop is intentionally lightweight:
    wake events are handled inline (fast, no LLM); audio chunks are handed
    off to _voice_worker threads so the loop stays responsive to new events.
    """
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=CHUNK_SIZE, callback=audio_callback):
        log.info("[STREAM] opened")

        while not stop_event.is_set() and not restart_event.is_set():
            try:
                msg = msg_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            if msg[0] == "wake":
                _, score = msg
                log.info("[WAKE] ● score=%.3f", score)
                _state["in_conversation"]  = True
                _state["tts_playing"]      = False
                _state["tts_since"]        = 0.0
                _state["cooldown_until"]   = 0.0
                _state["last_interaction"] = time.time()
                emit("stop_tts")
                emit("woke", score=score)

            elif msg[0] == "audio":
                _, audio, dur = msg
                log.debug("[AUDIO] %.2fs received", dur)
                threading.Thread(
                    target=_voice_worker,
                    args=(audio, stt),
                    daemon=True,
                    name="voice-worker",
                ).start()

        log.info("[STREAM] closed (stop=%s restart=%s)",
                 stop_event.is_set(), restart_event.is_set())

# ── WebSocket server ──────────────────────────────────────────────────────────

async def _ws_handler(websocket):
    _clients.add(websocket)
    log.info("[WS] client connected (%d total)", len(_clients))
    try:
        async for message in websocket:
            try:
                cmd = json.loads(message)
                t   = cmd.get("type", "")

                if t == "text":
                    txt = cmd.get("text", "").strip()
                    if txt:
                        log.info("[CHAT] received: '%s'", txt)
                        # Emit "heard" immediately so the user bubble appears
                        # in the UI before LLM processing begins (no perceived delay).
                        emit("heard", text=txt)
                        threading.Thread(target=_text_worker, args=(txt,),
                                         daemon=True, name="text-worker").start()

                elif t == "tts_done":
                    was_playing            = _state["tts_playing"]
                    _state["tts_playing"]  = False
                    _state["tts_since"]    = 0.0
                    _state["last_trig"]    = 0.0   # don't block next "hey jarvis"
                    _state["cooldown_until"] = time.time() + TTS_COOLDOWN
                    log.info("[TTS] done — VAD re-opens in %.1fs (in_conv=%s busy=%s)",
                             TTS_COOLDOWN, _state["in_conversation"], _state["busy"])
                    if not _state["busy"]:
                        emit("status", text="Listening…")

                elif t == "cancel_task":
                    if _task_state["active"]:
                        _task_state["cancel"].set()
                        log.info("[TASK] cancel requested")

                elif t == "stop":
                    stop_event.set()

            except Exception as exc:
                log.debug("[WS] parse error: %s", exc)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _clients.discard(websocket)
        log.info("[WS] client disconnected (%d remaining)", len(_clients))


async def _ws_main():
    global _event_loop
    _event_loop = asyncio.get_event_loop()

    threading.Thread(target=_run_voice, daemon=True, name="voice-pipeline").start()
    threading.Thread(target=_watchdog,  daemon=True, name="watchdog").start()

    log.info("[WS] listening on ws://localhost:%d", WS_PORT)
    async with websockets.serve(_ws_handler, "localhost", WS_PORT):
        await asyncio.Future()

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    bad = [w for w in WAKE_WORDS if w not in AVAILABLE]
    if bad:
        log.error("Unknown wake words: %s  Available: %s", bad, AVAILABLE)
        sys.exit(1)

    log.info("◉  JARVIS Voice Server")
    log.info("   Wake words  : %s", WAKE_WORDS)
    log.info("   LLM model   : %s", OLLAMA_MODEL)
    log.info("   Whisper     : %s", WHISPER_MODEL)
    log.info("   Threshold   : %s", WAKE_THRESHOLD)
    log.info("   TTS cooldown: %.1fs", TTS_COOLDOWN)

    try:
        asyncio.run(_ws_main())
    except KeyboardInterrupt:
        stop_event.set()
        log.info("Jarvis offline.")


if __name__ == "__main__":
    main()
