# JARVIS Mark II

> A fully local, voice-activated AI desktop assistant for macOS.  
> No cloud. No subscriptions. Everything runs on your machine.

![Platform](https://img.shields.io/badge/platform-macOS%2013%2B-black?style=flat-square)
![Python](https://img.shields.io/badge/python-3.11%2B-3776ab?style=flat-square)
![Swift](https://img.shields.io/badge/swift-5.9%2B-f05138?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![Offline](https://img.shields.io/badge/inference-fully%20local-blueviolet?style=flat-square)

---

JARVIS lives as a floating **Dynamic Island-style overlay** at the top of your screen. Say **"Hey Jarvis"** to wake it, ask anything, or give it a desktop task — it'll open apps, navigate the web, type for you, and report back step by step in a live progress card.

---

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running](#running)
- [Usage](#usage)
- [Configuration](#configuration)
- [Project Structure](#project-structure)
- [Automation Actions](#automation-actions)
- [Performance Notes](#performance-notes)
- [Known Limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Stack](#stack)

---

## Features

| | |
|---|---|
| Wake word | Always-on "Hey Jarvis" detection using on-device ONNX inference |
| Voice conversations | Continuous back-and-forth — no need to repeat the wake word each time |
| Local LLM | Streaming responses via Ollama (Llama 3.2 by default, fully swappable) |
| Desktop automation | Opens apps, navigates URLs, types text, clicks menus, moves files |
| Desktop 2 isolation | Tasks run in a separate Mission Control space — your workspace stays clean |
| Live progress | Floating card tracks every step in real time with a timer and cancel button |
| Text input | Hover the island to expand it and type instead of speaking |
| Focus restore | After automation, JARVIS returns focus to whatever app you were in |
| Fully offline | Wake word, STT, LLM — nothing leaves your machine |

---

## How It Works

```
Microphone
    │
    ▼
OpenWakeWord (ONNX)       ←  always listening, on-device
    │  "Hey Jarvis"
    ▼
faster-whisper (int8)     ←  local speech-to-text
    │
    ▼
Ollama / Llama 3.2        ←  streams tokens back in real time
    │
    ├── Conversation  →  AVSpeechSynthesizer speaks the reply
    │
    └── Task?  →  Task Planner (Ollama JSON mode)
                       │  generates step-by-step plan
                       ▼
                  Automation Engine  (AppleScript / osascript)
                       │  executes in Desktop 2
                       ▼
                  Progress Overlay   (live step updates → Swift UI)
```

The **Python voice server** and **Swift macOS app** communicate over a local WebSocket connection. Swift handles all UI and TTS; Python handles audio capture, STT, LLM inference, and automation.

---

## Requirements

### System

- macOS 13 Ventura or later
- Xcode 15+ (to build the Swift app)
- Python 3.11+
- [Ollama](https://ollama.com) installed and running

### Permissions

Grant these in **System Settings → Privacy & Security**:

| Permission | Why |
|---|---|
| Microphone | Voice input |
| Accessibility | Desktop automation via System Events |
| Screen Recording | Only needed for the `screenshot` automation action |

> Accessibility is the most important one — without it, AppleScript cannot control other apps and automation steps will silently fail.

---

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/your-username/jarvis-ast.git
cd jarvis-ast
```

### 2. Set up the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> First install takes a few minutes — `onnxruntime` and `faster-whisper` are large packages.

### 3. Pull an Ollama model

```bash
# Start Ollama if it isn't running
ollama serve

# Pull the default model
ollama pull llama3.2
```

Any model in your Ollama library works. See [Configuration](#configuration) to switch models.

### 4. Build the Swift app

```bash
cd swift-app
swift build
cd ..
```

---

## Running

Open **two terminals** from the project root.

**Terminal 1 — voice server**
```bash
source .venv/bin/activate
python voice_server.py
```

Wait for this line before starting the app:
```
✓ Say 'hey jarvis' to start | 'thank you jarvis' to stop
```

**Terminal 2 — macOS app**
```bash
./swift-app/.build/debug/Jarvis
```

The island appears at the **top center** of your screen. There's no Dock icon — the app runs as a menu bar accessory (waveform icon `⌘`) with a single Quit option.

> **First launch is slow.** Whisper downloads its model weights and OpenWakeWord downloads ONNX models on first run. After that, startup takes a few seconds.

---

## Usage

### Voice

| Say | Result |
|---|---|
| `Hey Jarvis` | Wakes up, starts listening |
| `Hey Jarvis, [question]` | Wake + ask in one breath |
| *(anything, after wake)* | Continues conversation — no wake word needed |
| `Thank you Jarvis` | Ends the session |
| `Goodbye Jarvis` | Also ends the session |
| `Stop listening` | Also ends the session |

Conversation sessions **auto-expire after 90 seconds of silence** — JARVIS goes back to wake-word-only mode automatically.

### Text

Hover over the island to expand it. Type in the input field and press **Return**.

### Automation

Say or type what you want in plain English — JARVIS figures out if it needs to do something on your desktop:

```
Open Spotify
Search for flights to New York next weekend
Take a screenshot
Create a new Google Doc called Project Notes
Open my Downloads folder
Search Google for the latest MacBook Pro reviews
Move report.pdf from Downloads to Documents
```

When a task is detected, JARVIS:
1. Confirms out loud what it's about to do
2. Switches to Desktop 2
3. Executes each step and streams progress to the overlay card
4. Switches back to Desktop 1
5. Restores focus to the app you were in

You can **Cancel** any running task from the progress card at any time.

---

## Configuration

Create a `.env` file in the project root:

```env
# ── LLM ───────────────────────────────────────────────
# Model used for chat responses
OLLAMA_MODEL=llama3.2

# Separate model for the automation task planner
# Defaults to OLLAMA_MODEL if not set
OLLAMA_PLANNER_MODEL=llama3.2

# ── Speech-to-text ────────────────────────────────────
# Whisper model size: tiny | base | small | medium | large
# tiny  → fastest, works well for clear speech
# small → better accuracy, ~2x slower
WHISPER_MODEL=tiny

# ── Wake word ─────────────────────────────────────────
# Sensitivity 0.0–1.0. Lower = more sensitive, more false triggers
WAKE_THRESHOLD=0.35

# Comma-separated list of wake words to enable
# Available: hey_jarvis, alexa, hey_mycroft, ok_nabu
JARVIS_WAKE_WORDS=hey_jarvis

# ── Automation ────────────────────────────────────────
# Set to 0 to disable desktop automation (faster for chat-only use)
ENABLE_AUTOMATION=1

# ── Network ───────────────────────────────────────────
# WebSocket port — must match between Python server and Swift app
JARVIS_WS_PORT=8765
```

---

## Project Structure

```
jarvis-ast/
│
├── voice_server.py        # Audio capture, VAD, wake word, STT, LLM, WebSocket server
├── task_planner.py        # Detects automation intent, generates step plans via Ollama JSON mode
├── automation_engine.py   # Executes steps via AppleScript / osascript
├── requirements.txt       # Python dependencies
├── .env                   # Your local config (create this — not committed)
│
└── swift-app/
    └── Sources/
        ├── JarvisApp.swift        # App entry point, menu bar item, window lifecycle
        ├── IslandPanel.swift      # Floating island overlay + hover-expand animation
        ├── ProgressOverlay.swift  # Bottom-right task progress card
        ├── VoiceViewModel.swift   # WebSocket client, shared state, TTS engine
        ├── Models.swift           # Mode, ChatMessage, TaskStep, RecentTask types
        └── ContentView.swift      # Shared UI helpers (StatusDot, blur, Color extension)
```

---

## Automation Actions

These are the actions the task planner can compose into a plan:

| Action | What it does | Params |
|---|---|---|
| `open_app` | Launch or focus an app | `{"app": "Safari"}` |
| `navigate_url` | Open a URL in the default browser | `{"url": "https://docs.google.com"}` |
| `type` | Type text (pastes via clipboard) | `{"text": "Hello world"}` |
| `shortcut` | Press a keyboard shortcut | `{"keys": "cmd+shift+n"}` |
| `click` | Click a UI element by label | `{"app": "Finder", "element": "New Folder"}` |
| `click_menu` | Click a menu bar item | `{"app": "Chrome", "menu": "File", "item": "New Tab"}` |
| `search_web` | Google search | `{"query": "paris weather this week"}` |
| `screenshot` | Capture the screen | `{}` |
| `wait` | Pause between steps | `{"seconds": 1.5}` |
| `file_op` | Open, copy, move, or delete files | `{"op": "move", "src": "/path/a", "dst": "/path/b"}` |
| `applescript` | Run a raw AppleScript | `{"script": "tell application ..."}` |
| `switch_space` | Switch Mission Control desktop | `{"number": 2}` |

---

## Performance Notes

- **Whisper `tiny`** is the default — fast enough for real-time use on Apple Silicon. Switch to `small` for better accuracy on quieter or accented speech.
- **Automation detection** uses a keyword regex gate before calling Ollama — purely conversational messages never hit the planner, so there's no extra latency for normal chat.
- **Desktop 2 switching** adds ~0.4s at the start and end of each task. If you don't need isolation, set `ENABLE_AUTOMATION=0` and re-enable per-task via direct AppleScript if needed.
- **Ollama speed** depends heavily on your hardware. On M-series Macs, Llama 3.2 3B runs comfortably in real time. Larger models (7B+) add noticeable latency.
- The **conversation history** is capped at 8 turns to keep context small and LLM calls fast. Adjust `MAX_HISTORY_TURNS` in `voice_server.py` if needed.

---

## Known Limitations

- **Automation accuracy** depends on the LLM's ability to generate correct AppleScript targets. Complex multi-app workflows may require retrying or rephrasing.
- **Desktop 2 switching** requires Mission Control keyboard shortcuts to be enabled. If they're off, automation runs in the current space instead (non-fatal).
- **`click` and `click_menu`** work best with standard macOS apps. Electron apps (Slack, VS Code, Notion) expose limited accessibility trees and may not respond to UI element clicks.
- **TTS voice** is the default macOS system voice. You can change it in System Settings → Accessibility → Spoken Content.
- The Swift app currently has no persistent settings UI — all config is via `.env`.

---

## Troubleshooting

<details>
<summary><strong>"Hey Jarvis" isn't triggering</strong></summary>

- Wait for `OWW ready` in the server logs before speaking
- Speak clearly at a normal pace — the model needs ~300ms of audio to score above threshold
- Lower `WAKE_THRESHOLD` to `0.25` in `.env` for a more sensitive trigger
- Make sure Microphone permission is granted to Terminal (or your IDE)

</details>

<details>
<summary><strong>Automation steps fail or do nothing</strong></summary>

- Grant **Accessibility** permission to Terminal in System Settings → Privacy & Security
- Relaunch the Python server after granting permissions
- Some apps (Electron-based) have limited AppleScript support — try rephrasing to use `navigate_url` or `search_web` instead

</details>

<details>
<summary><strong>Desktop 2 switching doesn't work</strong></summary>

- Open Mission Control and make sure you have at least 2 desktops
- Enable keyboard shortcuts: System Settings → Desktop & Dock → Mission Control → Keyboard & Mouse Shortcuts → Switch to Desktop 2

</details>

<details>
<summary><strong>Ollama errors on startup</strong></summary>

- Run `ollama serve` in a separate terminal before starting the voice server
- Run `ollama pull llama3.2` if you haven't downloaded the model yet
- Check `ollama list` to see what models are available locally

</details>

<details>
<summary><strong>Island doesn't appear / app crashes on launch</strong></summary>

- Confirm the Swift build succeeded: `cd swift-app && swift build`
- Make sure the Python voice server is running — the Swift app connects on launch and won't show the island until the WebSocket is available
- Check the terminal running `voice_server.py` for any Python errors

</details>

<details>
<summary><strong>Responses are too slow</strong></summary>

- Switch to `WHISPER_MODEL=tiny` in `.env` (default)
- Try a smaller Ollama model: `ollama pull llama3.2:1b`
- Set `ENABLE_AUTOMATION=0` if you only need chat — skips the planner call entirely
- Close other GPU/CPU-heavy apps while using JARVIS

</details>

---

## Stack

| Layer | Technology |
|---|---|
| UI & App | SwiftUI + AppKit (macOS native) |
| Wake word | [OpenWakeWord](https://github.com/dscripka/openWakeWord) (ONNX) |
| Speech-to-text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (int8 CPU) |
| LLM | [Ollama](https://ollama.com) — Llama 3.2 (swappable) |
| Text-to-speech | AVSpeechSynthesizer (macOS built-in) |
| Automation | AppleScript via `osascript` |
| Transport | WebSockets (`websockets` ↔ `URLSessionWebSocketTask`) |

---

## License

MIT — do whatever you want with it.
