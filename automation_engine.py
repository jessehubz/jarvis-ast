#!/usr/bin/env python3
"""
Automation Engine — executes task steps using AppleScript / System Events.
"""

import base64
import logging
import os
import subprocess
import tempfile
import time
import urllib.parse

log = logging.getLogger("jarvis.automation")

_TIMEOUT = 30  # seconds per osascript call


def _apple(script: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        return (True, r.stdout.strip()) if r.returncode == 0 else (False, r.stderr.strip())
    except subprocess.TimeoutExpired:
        return False, "AppleScript timed out"
    except Exception as exc:
        return False, str(exc)


# ── Actions ───────────────────────────────────────────────────────────────────

def open_app(app: str) -> tuple[bool, str]:
    ok, msg = _apple(f'tell application "{app}" to activate')
    if ok:
        time.sleep(0.6)  # reduced from 1.2s — enough for most apps to become frontmost
    return ok, msg or f"Opened {app}"


def type_text(text: str) -> tuple[bool, str]:
    """Paste via clipboard — reliable for any length of text."""
    try:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=5, check=True)
    except Exception as exc:
        return False, f"pbcopy failed: {exc}"
    return _apple('tell application "System Events" to keystroke "v" using command down')


def press_shortcut(keys: str) -> tuple[bool, str]:
    """keys format: 'cmd+c', 'cmd+shift+t', 'return', 'escape', etc."""
    parts   = keys.lower().replace(" ", "").split("+")
    key     = parts[-1]
    mods    = parts[:-1]

    mod_map = {
        "cmd":     "command down", "command": "command down",
        "shift":   "shift down",
        "opt":     "option down",  "option":  "option down", "alt": "option down",
        "ctrl":    "control down", "control": "control down",
    }
    # Build modifier string
    mod_items = [mod_map[m] for m in mods if m in mod_map]
    mod_str   = "{" + ", ".join(mod_items) + "}" if mod_items else ""

    # Key codes for non-printable keys
    key_codes = {
        "return": 36, "enter": 36, "tab": 48, "space": 49,
        "delete": 51, "backspace": 51, "escape": 53,
        "left": 123,  "right": 124,  "down": 125, "up": 126,
    }
    if key in key_codes:
        clause = f"key code {key_codes[key]}"
    else:
        clause = f'keystroke "{key}"'

    using = f" using {mod_str}" if mod_str else ""
    return _apple(f'tell application "System Events" to {clause}{using}')


def click_element(app: str, element: str) -> tuple[bool, str]:
    script = f'''
tell application "System Events"
    tell process "{app}"
        set frontmost to true
        try
            click button "{element}" of window 1
        on error
            click UI element "{element}" of window 1
        end try
    end tell
end tell'''
    return _apple(script)


def click_menu(app: str, menu: str, item: str) -> tuple[bool, str]:
    script = f'''
tell application "{app}" to activate
tell application "System Events"
    tell process "{app}"
        click menu item "{item}" of menu "{menu}" of menu bar 1
    end tell
end tell'''
    return _apple(script)


def navigate_url(url: str) -> tuple[bool, str]:
    """Open a URL directly in the default browser — faster than open_app + type."""
    return _apple(f'open location "{url}"')


def search_web(query: str) -> tuple[bool, str]:
    url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
    return _apple(f'open location "{url}"')


def take_screenshot() -> tuple[bool, str]:
    """Returns (True, base64_jpeg) or (False, error_message)."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp = f.name
        r = subprocess.run(
            ["screencapture", "-x", "-t", "jpg", tmp],
            capture_output=True, timeout=10,
        )
        if r.returncode != 0:
            return False, "screencapture failed"
        with open(tmp, "rb") as f:
            return True, base64.b64encode(f.read()).decode()
    except Exception as exc:
        return False, str(exc)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def wait_action(seconds: float) -> tuple[bool, str]:
    s = max(0.1, min(float(seconds), 30.0))
    time.sleep(s)
    return True, f"Waited {s:.1f}s"


def file_op(op: str, src: str, dst: str = "") -> tuple[bool, str]:
    op = op.lower()
    if op == "open":
        return _apple(f'tell application "Finder" to open POSIX file "{src}"')
    if op in ("copy", "move"):
        verb = "copy" if op == "copy" else "move"
        return _apple(f'''
tell application "Finder"
    {verb} POSIX file "{src}" to POSIX file "{dst}"
end tell''')
    if op == "delete":
        return _apple(f'tell application "Finder" to delete POSIX file "{src}"')
    return False, f"Unknown file_op: {op}"


# ── Space switching ───────────────────────────────────────────────────────────

# Control + number key codes for switching Mission Control spaces (1-indexed)
_SPACE_KEY_CODES = {1: 18, 2: 19, 3: 20, 4: 21, 5: 23}


def switch_space(number: int) -> tuple[bool, str]:
    """Switch to Mission Control Desktop space N (requires accessibility permissions)."""
    code = _SPACE_KEY_CODES.get(number)
    if not code:
        return False, f"No key code for space {number}"
    ok, msg = _apple(
        f'tell application "System Events" to key code {code} using {{control down}}'
    )
    if ok:
        time.sleep(0.4)  # wait for space-transition animation
    return ok, msg or f"Switched to Desktop {number}"


# ── Focus management ─────────────────────────────────────────────────────────

def get_frontmost_app() -> str:
    """Return the name of the currently frontmost application."""
    ok, name = _apple(
        'tell application "System Events" to get name of '
        'first application process whose frontmost is true'
    )
    return name if ok else ""


def restore_focus(app_name: str) -> None:
    """Re-activate an app that was frontmost before a task ran."""
    if app_name:
        _apple(f'tell application "{app_name}" to activate')


# ── Dispatcher ────────────────────────────────────────────────────────────────

def execute_step(step: dict) -> tuple[bool, str]:
    """Execute one task step. Returns (success, message_or_base64_for_screenshot)."""
    action = step.get("action", "")
    p      = step.get("params", {})
    log.info("[AUTO] %s — %s %s", step.get("name", "?"), action, p)

    if action == "open_app":     return open_app(p.get("app", ""))
    if action == "type":          return type_text(p.get("text", ""))
    if action == "shortcut":     return press_shortcut(p.get("keys", ""))
    if action == "click":        return click_element(p.get("app", ""), p.get("element", ""))
    if action == "click_menu":   return click_menu(p.get("app", ""), p.get("menu", ""), p.get("item", ""))
    if action == "screenshot":   return take_screenshot()
    if action == "search_web":   return search_web(p.get("query", ""))
    if action == "wait":         return wait_action(float(p.get("seconds", 1.0)))
    if action == "applescript":  return _apple(p.get("script", ""))
    if action == "file_op":      return file_op(p.get("op", ""), p.get("src", ""), p.get("dst", ""))
    if action == "switch_space":  return switch_space(int(p.get("number", 1)))
    if action == "navigate_url":  return navigate_url(p.get("url", ""))

    return False, f"Unknown action: {action}"
