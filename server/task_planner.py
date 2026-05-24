#!/usr/bin/env python3
"""
Task Planner — detects automation intent and generates step plans via Ollama JSON mode.
Single Ollama call: returns {"is_automation": false} for normal chat, or the full plan.
"""

import json
import logging
import os

import ollama

log = logging.getLogger("jarvis.planner")

PLANNER_MODEL = os.getenv("OLLAMA_PLANNER_MODEL", os.getenv("OLLAMA_MODEL", "llama3.2"))

_SYSTEM_PROMPT = """\
You are an automation planner for Jarvis, a macOS AI assistant.

Decide if the user's request requires desktop automation (opening apps, clicking UI,
typing text, searching the web, taking screenshots, moving files, running scripts, etc.)
or if it is a normal conversation / question.

If it is NOT automation, respond with ONLY:
{"is_automation": false}

If it IS automation, respond with ONLY:
{
  "is_automation": true,
  "verbal_response": "One sentence telling the user what you are about to do (for TTS)",
  "task_name": "Short task name (3–5 words)",
  "estimated_seconds": 10,
  "steps": [
    {
      "id": "step_1",
      "name": "Human-readable step label",
      "action": "open_app",
      "params": {"app": "Safari"}
    }
  ]
}

Available actions and their params:
- open_app      {"app": "AppName"}
- navigate_url  {"url": "https://..."}   ← preferred over open_app for web pages; faster
- type          {"text": "text to type"}
- shortcut      {"keys": "cmd+c"}
- click         {"app": "AppName", "element": "button label"}
- click_menu    {"app": "AppName", "menu": "File", "item": "New Tab"}
- screenshot    {}
- search_web    {"query": "search query"}
- wait          {"seconds": 1.5}
- applescript   {"script": "tell application ..."}
- file_op       {"op": "open|copy|move|delete", "src": "/path", "dst": "/path"}

Prefer navigate_url over (open_app + type URL + shortcut) whenever the destination URL is known.

Use screenshot as the LAST step whenever you open something the user wants to see,
so they get visual confirmation.
"""


def plan_automation(text: str) -> dict:
    """
    Detect automation intent and generate a step plan in one Ollama call.
    Returns the plan dict (is_automation=True) or {"is_automation": False}.
    """
    try:
        resp = ollama.chat(
            model=PLANNER_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": text},
            ],
            format="json",
            options={"temperature": 0.05},
        )
        result = json.loads(resp["message"]["content"])
        if result.get("is_automation"):
            log.info("[PLANNER] automation: '%s' (%d steps)",
                     result.get("task_name"), len(result.get("steps", [])))
        else:
            log.debug("[PLANNER] not automation")
        return result
    except Exception as exc:
        log.error("[PLANNER] failed: %s", exc)
        return {"is_automation": False}
