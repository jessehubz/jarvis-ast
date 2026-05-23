#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JARVIS — Voice Demo                                                         ║
╚══════════════════════════════════════════════════════════════════════════════╝

WHAT THIS DOES
  1. Calibrates mic sensitivity at startup (1-second ambient noise reading)
  2. Listens continuously for the wake word "hey jarvis"
  3. Records your command/question until you go quiet
  4. Transcribes speech → text with Whisper (runs locally, free)
  5. Sends the text to Ollama (local AI model, free, unlimited)
  6. Streams Jarvis's response to the terminal word by word
  7. Remembers the conversation so follow-up questions work
  8. Say "thank you jarvis" to exit

HOW TO RUN
  First-time setup:
    brew install ollama
    ollama pull llama3.2          # ~2 GB download, do this once
    ollama serve                  # start the server, keep this running

  Then in a new terminal:
    source .venv/bin/activate
    python voice_demo.py

CONFIG  (edit .env to change these)
  JARVIS_WAKE_WORD   hey_jarvis | alexa | hey_mycroft | ok_nabu
                     default: hey_jarvis

  OLLAMA_MODEL       any model pulled via "ollama pull <name>"
                     default: llama3.2
                     smaller/faster: llama3.2:1b, mistral, phi3

  WHISPER_MODEL      tiny | base | small | medium
                     default: base

  WAKE_THRESHOLD     sensitivity 0.0–1.0  (lower = triggers more easily)
                     default: 0.5

HOW IT WORKS (overview)
  A single audio stream runs the whole time using a callback function.
  The callback runs every 80ms on its own background thread and does two jobs
  depending on the current mode:

    IDLE mode     → feed each audio chunk to the wake word model.
                    If it hears "hey jarvis", switch to RECORDING mode.

    RECORDING mode → save each audio chunk into a list.
                    If the room goes quiet (or 15 seconds pass), bundle all
                    the chunks into one recording and drop it in msg_queue.

  The main thread sits in a loop reading from msg_queue:
    "wake" message  → show ● REC, start a spinner
    "audio" message → stop spinner, transcribe with Whisper,
                      send text to Ollama, print the streamed response

  Conversation history is kept in memory, so follow-up questions like
  "tell me more" or "what was the second option?" work without repeating context.

EXIT
  Say "thank you jarvis"   or   press Ctrl+C
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
#  Values are read from .env first, then fall back to the defaults shown here.
# ══════════════════════════════════════════════════════════════════════════════

WHISPER_MODEL  = os.getenv("WHISPER_MODEL", "base")
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.5"))
OLLAMA_MODEL   = os.getenv("OLLAMA_MODEL", "llama3.2")

SAMPLE_RATE    = 16000   # samples per second — openWakeWord requires exactly 16000
CHUNK_SIZE     = 1280    # samples per audio chunk = 80ms at 16000 Hz
SILENCE_SEC    = 1.5     # how many seconds of quiet ends a recording
MIN_SPEECH_SEC = 0.3     # recordings shorter than this are treated as accidental triggers
MAX_RECORD_SEC = 15      # absolute maximum recording length (fallback if silence never hits)

EXIT_PHRASE    = "thank you jarvis"

# All wake words openWakeWord ships with pre-trained (no custom training needed)
AVAILABLE_WORDS = ["hey_jarvis", "alexa", "hey_mycroft", "ok_nabu"]

# JARVIS_WAKE_WORDS accepts one or more words from AVAILABLE_WORDS, comma-separated.
# Any one of them firing will activate Jarvis.
# Example in .env:  JARVIS_WAKE_WORDS=hey_jarvis,ok_nabu
_raw = os.getenv("JARVIS_WAKE_WORDS", "hey_jarvis")
WAKE_WORDS = [w.strip() for w in _raw.split(",") if w.strip()]

# Validate every word in the list
_bad = [w for w in WAKE_WORDS if w not in AVAILABLE_WORDS]
if _bad:
    print(f"Unknown wake word(s): {_bad}. Choose from: {AVAILABLE_WORDS}")
    sys.exit(1)

# Pre-compute how many chunks equal the silence/max durations
_SILENCE_CHUNKS = int(SILENCE_SEC * SAMPLE_RATE / CHUNK_SIZE)   # ~18 chunks = 1.5s
_MAX_CHUNKS     = int(MAX_RECORD_SEC * SAMPLE_RATE / CHUNK_SIZE) # ~187 chunks = 15s

# The personality Jarvis uses in every conversation.
# This is the "system" message that sets the tone before any user message.
SYSTEM_PROMPT = (
    "You are Jarvis, a concise and helpful personal AI assistant running locally on macOS. "
    "Keep answers brief and conversational unless the user asks for detail. "
    "Never say you're an AI or mention your model name."
)

# ══════════════════════════════════════════════════════════════════════════════
#  TERMINAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

# ANSI escape codes — these are special characters that tell the terminal
# to change text colour or style. \033[ starts the code, 0m resets it.
RESET  = "\033[0m"   # back to normal
BOLD   = "\033[1m"   # bold text
DIM    = "\033[2m"   # dimmed/grey text
CYAN   = "\033[96m"  # bright cyan
GREEN  = "\033[92m"  # bright green
YELLOW = "\033[93m"  # bright yellow
RED    = "\033[91m"  # bright red

def style(text, *codes):
    """
    Wrap text in one or more ANSI colour/style codes.

    Usage:
        style("hello", BOLD, CYAN)   →  bold cyan "hello"
        style("error", RED)          →  red "error"

    The RESET at the end makes sure colours don't bleed into the next print.
    """
    return "".join(codes) + text + RESET


def normalize(text: str) -> str:
    """
    Strip all punctuation and convert to lowercase.

    This is used before checking for the exit phrase so that Whisper's
    punctuation doesn't break the match. For example:
        "Thank you, Jarvis."  →  "thank you jarvis"   ✓ matches EXIT_PHRASE
        Without this step the comma would cause the check to fail.
    """
    return re.sub(r"[^a-z\s]", "", text.lower()).strip()


def rms(audio: np.ndarray) -> float:
    """
    Calculate the RMS (Root Mean Square) amplitude of an audio chunk.

    RMS is a standard way to measure loudness. A chunk of silence has an
    RMS close to 0. Speech typically reads 0.05–0.3. Background room noise
    sits somewhere in between, which is why we calibrate at startup.

    The tiny +1e-9 prevents a math error if the chunk is completely silent.
    """
    return float(np.sqrt(np.mean(audio.astype(np.float32) ** 2) + 1e-9))

# ══════════════════════════════════════════════════════════════════════════════
#  MIC CALIBRATION
# ══════════════════════════════════════════════════════════════════════════════

def calibrate_silence() -> float:
    """
    Record ~1 second of ambient room noise and return a silence threshold.

    WHY THIS IS NEEDED
      The silence detector in audio_callback checks whether the microphone
      level has dropped below a threshold. If we use a fixed threshold
      (e.g. 0.015), it will be wrong for most rooms:
        - A quiet room: 0.015 may be too HIGH → never detects silence → recording
          runs all the way to the 15-second hard cap before stopping.
        - A noisy room: 0.015 may be too LOW → detects "silence" mid-sentence
          and cuts you off early.

      By measuring the actual background noise in your room at startup,
      we set the threshold to 2.5× that level — above the noise floor but
      low enough that real silence (when you stop talking) is still detected.

    HOW IT WORKS
      Opens a short audio stream using the callback API (not blocking reads,
      which can hang on macOS). Collects ~12 chunks of 80ms each (≈ 1 second),
      calculates the average RMS, then closes the stream.

    RETURNS
      A float — the silence threshold to use for this session.
      Example: ambient=0.012 → returns max(0.02, 0.012 × 2.5) = 0.03
    """
    print(style("  Calibrating mic — stay quiet for 1 second…", DIM), flush=True)

    levels   = []
    done     = threading.Event()
    n_chunks = max(1, int(SAMPLE_RATE / CHUNK_SIZE))  # ~12 chunks ≈ 1 second

    def _cb(indata, _f, _t, _s):
        # Collect RMS of each chunk until we have enough samples
        levels.append(rms(indata.flatten()))
        if len(levels) >= n_chunks:
            done.set()  # signal that we're done collecting

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=CHUNK_SIZE, callback=_cb):
        done.wait(timeout=5.0)  # wait up to 5s; gives up if mic never fires

    ambient   = float(np.mean(levels)) if levels else 0.03
    threshold = max(0.02, ambient * 2.5)
    print(style(f"  done — ambient={ambient:.4f}  →  threshold={threshold:.4f}", DIM))
    return threshold

# ══════════════════════════════════════════════════════════════════════════════
#  SHARED STATE  (audio callback thread  <->  main thread)
# ══════════════════════════════════════════════════════════════════════════════

# msg_queue is how the audio callback thread talks to the main thread.
# It holds two kinds of tuples:
#   ("wake",  score)          — wake word was detected; score is confidence 0–1
#   ("audio", array, seconds) — recording is done; array is the raw audio data
msg_queue  = queue.Queue()

# stop_event is set to True when it's time to shut everything down.
# Both the main loop and the audio callback check this.
stop_event = threading.Event()

# _cb holds the state of the audio callback's state machine.
# It's a plain dict (not a class) because it's only ever touched by
# the callback function running on sounddevice's background thread.
_cb = {
    "mode":        "idle",   # current state: "idle" or "recording"
    "chunks":      [],       # list of audio arrays collected during recording
    "silence_cnt": 0,        # how many consecutive quiet chunks we've seen
    "last_trig":   0.0,      # timestamp of the last wake trigger (prevents double-fire)
    "rec_start":   0.0,      # timestamp when the current recording started
    "silence_thr": 0.03,     # silence threshold — overwritten by calibrate_silence()
    "oww":         None,     # the openWakeWord model object — set in main()
}

# ══════════════════════════════════════════════════════════════════════════════
#  AUDIO CALLBACK
# ══════════════════════════════════════════════════════════════════════════════

def audio_callback(indata, _frames, _time_info, _status):
    """
    Called automatically by sounddevice every 80ms with a fresh audio chunk.

    This is the core of the whole pipeline. It runs on sounddevice's
    background thread — NOT the main thread — so it must be fast.
    Never do slow work here (no Whisper, no Ollama, no file reads).

    PARAMETERS
      indata  — numpy array of shape (1280, 1), dtype int16.
                Contains 80ms of raw microphone audio.
      _frames, _time_info, _status — required by sounddevice's API but unused.

    STATE MACHINE
      IDLE mode:
        Feed the audio chunk to the openWakeWord model.
        If the confidence score for "hey_jarvis" crosses WAKE_THRESHOLD,
        switch to RECORDING mode and put a ("wake", score) message in msg_queue
        so the main thread knows to show the ● REC indicator.
        The 1-second cooldown (last_trig check) prevents the same wake word
        utterance from firing twice.

      RECORDING mode:
        Append each chunk to _cb["chunks"].
        After each chunk, check if the room has gone quiet:
          - If the RMS level is below silence_thr → increment silence_cnt
          - If the RMS level is above silence_thr → reset silence_cnt to 0
        Recording ends when:
          A) silence_cnt reaches _SILENCE_CHUNKS (1.5 seconds of quiet), OR
          B) the total chunk count hits _MAX_CHUNKS (15-second hard cap)
        When done, concatenate all chunks into one numpy array and put an
        ("audio", array, duration) message in msg_queue for the main thread.
    """
    if stop_event.is_set():
        return

    chunk = indata.flatten().copy()   # flatten (1280,1) → (1280,), make a copy
    level = rms(chunk)                # measure loudness of this 80ms window

    if _cb["mode"] == "idle":
        # Get scores for every loaded wake word and take the highest one.
        # If any of them crosses the threshold, Jarvis wakes up.
        preds = _cb["oww"].predict(chunk)
        score = max((preds.get(w, 0.0) for w in WAKE_WORDS), default=0.0)
        now   = time.time()
        if score >= WAKE_THRESHOLD and (now - _cb["last_trig"]) > 1.0:
            _cb.update(mode="recording", chunks=[], silence_cnt=0,
                       last_trig=now, rec_start=now)
            msg_queue.put(("wake", score))

    else:  # recording mode
        _cb["chunks"].append(chunk)
        # Increment silence counter if quiet, reset if speech detected
        _cb["silence_cnt"] = _cb["silence_cnt"] + 1 if level <= _cb["silence_thr"] else 0

        recording_done = (
            _cb["silence_cnt"] >= _SILENCE_CHUNKS or   # natural end of speech
            len(_cb["chunks"]) >= _MAX_CHUNKS           # safety cap
        )
        if recording_done:
            audio    = np.concatenate(_cb["chunks"])
            duration = time.time() - _cb["rec_start"]
            _cb["mode"]   = "idle"
            _cb["chunks"] = []
            msg_queue.put(("audio", audio, duration))

# ══════════════════════════════════════════════════════════════════════════════
#  RECORDING SPINNER
# ══════════════════════════════════════════════════════════════════════════════

def recording_spinner(stop_spin: threading.Event):
    """
    Print a live spinner with elapsed time while a recording is in progress.
    Runs on its own daemon thread so it doesn't block the main thread.

    Uses \\r (carriage return) to overwrite the same terminal line each tick,
    giving the appearance of an updating counter rather than a scrolling log.
    When stop_spin is set, it clears the line before returning so the next
    print starts cleanly.

    PARAMETER
      stop_spin — a threading.Event. The main thread calls stop_spin.set()
                  when the recording is done, which causes this loop to exit.
    """
    frames = ["◐", "◓", "◑", "◒"]  # rotating quarter-circle spinner
    start  = time.time()
    i = 0
    while not stop_spin.is_set():
        print(f"\r  {frames[i % 4]} Recording…  {time.time() - start:.1f}s",
              end="", flush=True)
        i += 1
        time.sleep(0.2)
    print("\r" + " " * 32 + "\r", end="", flush=True)  # clear the spinner line

# ══════════════════════════════════════════════════════════════════════════════
#  TRANSCRIBE
# ══════════════════════════════════════════════════════════════════════════════

def transcribe(audio: np.ndarray, model) -> str:
    """
    Convert a raw audio recording into text using OpenAI Whisper.

    Whisper doesn't accept numpy arrays directly — it needs a file path.
    So we write the audio to a temporary WAV file, run transcription on it,
    then delete the file whether or not transcription succeeded.

    PARAMETERS
      audio — int16 numpy array from the audio callback (raw microphone data)
      model — the loaded Whisper model object (created once in main())

    RETURNS
      The transcribed string, e.g. "what's the weather like today"
      Returns "" if the audio is too short or if Whisper throws an error.

    WAV FILE FORMAT
      channels=1  (mono — one microphone)
      sampwidth=2 (16-bit audio = 2 bytes per sample)
      framerate   (16000 Hz — must match SAMPLE_RATE)
    """
    if len(audio) < SAMPLE_RATE * MIN_SPEECH_SEC:
        return ""  # too short to be a real command, skip it

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name  # get the file path before closing

    try:
        # Write the raw int16 audio bytes into a proper WAV container
        with wave.open(tmp, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio.tobytes())

        # Run Whisper — this is the slow step (1–5 seconds depending on model)
        return model.transcribe(tmp, language="en", fp16=False)["text"].strip()

    except Exception as e:
        print(style(f"\n  [Whisper error] {e}", RED))
        return ""

    finally:
        os.unlink(tmp)  # always delete the temp file, even if transcription failed

# ══════════════════════════════════════════════════════════════════════════════
#  ASK JARVIS
# ══════════════════════════════════════════════════════════════════════════════

def ask_jarvis(text: str, history: list) -> str:
    """
    Send the user's message to Ollama and stream the response to the terminal.

    PARAMETERS
      text    — the transcribed string from Whisper, e.g. "what is Python?"
      history — the running conversation list. Each item is a dict with
                "role" ("system", "user", or "assistant") and "content" (the text).
                We append to this list so future calls have full context.

    HOW CONVERSATION HISTORY WORKS
      Ollama (and most LLM APIs) take the entire conversation as input each time,
      not just the latest message. By keeping history across calls, Jarvis can
      answer follow-up questions that reference earlier ones. For example:
        You:    "who invented the telephone?"
        Jarvis: "Alexander Graham Bell."
        You:    "when was he born?"    ← Jarvis knows "he" = Bell because
                                          the previous exchange is in history

    STREAMING
      Instead of waiting for the full response before printing, we use
      stream=True which gives us one token (word fragment) at a time.
      Each token is printed immediately with end="" so they appear inline,
      building the response word by word in real time.

    RETURNS
      The full response text as a single string.
      Also appends both the user message and assistant response to history.
      Returns "" if Ollama is unreachable or throws an error.
    """
    history.append({"role": "user", "content": text})

    print(style("\n  Jarvis: ", CYAN, BOLD), end="", flush=True)

    full_response = ""
    try:
        stream = ollama.chat(model=OLLAMA_MODEL, messages=history, stream=True)
        for chunk in stream:
            token = chunk["message"]["content"]
            print(token, end="", flush=True)   # print each word as it arrives
            full_response += token
        print("\n")  # newline after the response finishes

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
    """
    Entry point. Loads all models, starts the audio stream, and runs the
    main event loop.

    STARTUP SEQUENCE
      1. Validate config (wake word must be a supported value)
      2. Calibrate mic silence threshold (1-second ambient noise reading)
      3. Load Whisper speech-to-text model into memory
      4. Load openWakeWord wake word detection model
      5. Verify Ollama is running and the requested model is downloaded
      6. Open the audio stream (callback-based, runs in background)
      7. Enter the main event loop

    MAIN EVENT LOOP
      Sits in a while loop calling msg_queue.get(timeout=0.2).
      The 0.2-second timeout means even if no audio events arrive,
      the loop wakes up every 200ms to check whether stop_event is set
      (this is what makes Ctrl+C respond quickly).

      On "wake" message  → show ● REC, start spinner thread
      On "audio" message → stop spinner, transcribe, check exit phrase,
                           send to Ollama, print response
    """
    # Format wake words for display: "hey_jarvis, ok_nabu" → "HEY JARVIS  /  OK NABU"
    word_display = "  /  ".join(w.replace("_", " ").upper() for w in WAKE_WORDS)

    # ── Header ────────────────────────────────────────────────────────────────
    print(style("\n  ◉  JARVIS", BOLD, CYAN))
    print(style("  " + "─" * 44, DIM))
    print(f"  Wake words: {style(word_display, BOLD)}")
    print(f"  AI model  : {style(OLLAMA_MODEL, BOLD)}  (via Ollama)")
    print(f"  Whisper   : {style(WHISPER_MODEL, BOLD)}")
    print(f"  Exit      : {style(EXIT_PHRASE.upper(), BOLD)}")
    print(style("  " + "─" * 44, DIM) + "\n")

    # ── Calibrate mic ─────────────────────────────────────────────────────────
    # Must run before the main stream opens — macOS only allows one input
    # stream at a time, so calibration borrows the mic briefly then releases it.
    silence_thr        = calibrate_silence()
    _cb["silence_thr"] = silence_thr

    # ── Load Whisper ──────────────────────────────────────────────────────────
    # whisper.load_model() downloads the model on first run (~145 MB for "base"),
    # then caches it at ~/.cache/whisper/ for future runs.
    print(style(f"  Loading Whisper [{WHISPER_MODEL}]…", DIM))
    stt_model = whisper.load_model(WHISPER_MODEL)
    print(style("  Whisper ready.", GREEN))

    # ── Load openWakeWord ─────────────────────────────────────────────────────
    # download_models() fetches the ONNX wake word model files on first run
    # (~30 MB), then reuses the cached files on subsequent runs.
    print(style("  Checking wake word models…", DIM))
    openwakeword.utils.download_models()
    _cb["oww"] = Model(wakeword_models=WAKE_WORDS, inference_framework="onnx")
    print(style("  Wake word detector ready.", GREEN))

    # ── Check Ollama is reachable (fast — no model loading) ──────────────────
    # ollama.list() just reads the local model registry file. It does NOT
    # load any model into memory, so it returns instantly.
    print(style(f"  Checking Ollama [{OLLAMA_MODEL}]…", DIM))
    try:
        available = [m.model for m in ollama.list().models]
        # Ollama appends ":latest" to model names (e.g. "llama3.2:latest"),
        # so we check if OLLAMA_MODEL appears anywhere in the name string.
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

    # ── Conversation history ──────────────────────────────────────────────────
    # Starts with the system prompt that defines Jarvis's personality.
    # Every user message and Jarvis reply gets appended here so that Ollama
    # receives the full context on every call.
    history = [{"role": "system", "content": SYSTEM_PROMPT}]

    # ── Ctrl+C handler ────────────────────────────────────────────────────────
    # signal.signal() tells Python: when the user presses Ctrl+C, call _sigint
    # instead of crashing immediately. _sigint sets stop_event which causes the
    # main loop and the audio callback to exit cleanly.
    def _sigint(_s, _f):
        print(style("\n\n  Shutting down…", DIM))
        stop_event.set()
    signal.signal(signal.SIGINT, _sigint)

    print(style(f'  Say "{word_display}" then ask anything', BOLD))
    print(style(f'  Say "{EXIT_PHRASE.upper()}" to quit\n', DIM))

    spin_stop:   threading.Event  | None = None
    spin_thread: threading.Thread | None = None

    # ── Open audio stream and enter event loop ────────────────────────────────
    # The "with" block keeps the stream open for the duration of the loop.
    # audio_callback() is called automatically by sounddevice every 80ms.
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                        blocksize=CHUNK_SIZE, callback=audio_callback):

        while not stop_event.is_set():
            try:
                msg = msg_queue.get(timeout=0.2)  # blocks for up to 200ms
            except queue.Empty:
                continue  # nothing in the queue yet, loop back

            kind = msg[0]

            # Wake word was detected — show indicator, start spinner
            if kind == "wake":
                _, score = msg
                ts = time.strftime("%H:%M:%S")
                print(style(f"  [{ts}] ", DIM)
                      + style("● REC", GREEN, BOLD)
                      + style(f"  confidence {score:.0%}", DIM))

                # Spinner runs on a daemon thread so it can animate freely
                # while the main thread waits for the next queue message
                spin_stop   = threading.Event()
                spin_thread = threading.Thread(
                    target=recording_spinner, args=(spin_stop,), daemon=True)
                spin_thread.start()

            # Recording finished — transcribe and respond
            elif kind == "audio":
                _, audio, duration = msg

                # Stop and clean up the spinner thread
                if spin_stop:   spin_stop.set()
                if spin_thread: spin_thread.join(timeout=0.5)

                if len(audio) < SAMPLE_RATE * MIN_SPEECH_SEC:
                    print(style("  (too short — try again)\n", DIM))
                    continue

                print(style(f"  Captured {duration:.1f}s — transcribing…", DIM))
                text = transcribe(audio, stt_model)

                if not text:
                    print(style("  (nothing transcribed — try again)\n", YELLOW))
                    continue

                print(style("  You: ", DIM) + style(f'"{text}"', BOLD))

                # Check for exit phrase before sending to Ollama
                if EXIT_PHRASE in normalize(text):
                    ask_jarvis("The user is saying goodbye. Say a brief farewell.", history)
                    stop_event.set()
                    break

                # Check for time-aware greetings.
                # openWakeWord only supports 4 pre-trained wake words, so phrases
                # like "good morning jarvis" can't be wake words themselves.
                # Instead we detect them here after transcription and reply
                # with a contextual greeting based on the current hour.
                clean = normalize(text)
                is_greeting = (
                    any(g in clean for g in ("good morning", "good afternoon", "good evening", "good night"))
                    and "jarvis" in clean
                )
                if is_greeting:
                    hour = time.localtime().tm_hour
                    if 5 <= hour < 12:
                        time_of_day = "morning"
                    elif 12 <= hour < 17:
                        time_of_day = "afternoon"
                    elif 17 <= hour < 22:
                        time_of_day = "evening"
                    else:
                        time_of_day = "night"
                    ask_jarvis(
                        f"The user greeted you with '{text}'. "
                        f"It is currently {time_of_day}. Respond with a warm, brief greeting.",
                        history
                    )
                    print(style(f'  Waiting for "{word_display}"…\n', DIM))
                    continue

                # Send to Ollama — response streams to terminal token by token
                ask_jarvis(text, history)

                print(style(f'  Waiting for "{word_display}"…\n', DIM))

    print(style("  Jarvis offline.\n", DIM))


if __name__ == "__main__":
    main()
