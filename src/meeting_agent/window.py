"""Buddy's floating window: one place to follow Buddy and talk to it.

It shows what `LiveView` gathers — the meeting as it is said, commands as they are
dictated or typed, and what the Claude Code session does with them — and hands what
the user types to the listener, which passes it on like a spoken command. The window
thinks about nothing: the session watching Buddy stays the brain.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

from .listener import (
    _process_started,
    _read_json,
    _write_json,
    approve_action,
    listener_state,
    process_alive,
    request,
    submit,
)
from .live import LiveView

def copy_text(text: str) -> bool:
    """Put text on the clipboard with the system's own tool; False when there is none."""
    import shutil
    import subprocess
    import sys

    tool = ["pbcopy"] if sys.platform == "darwin" else ["clip"] if sys.platform == "win32" else ["xclip", "-selection", "clipboard"]
    if shutil.which(tool[0]) is None:
        return False
    try:
        subprocess.run(tool, input=text.encode("utf-8"), check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def open_link(url: str) -> bool:
    import webbrowser

    if not url.startswith(("https://", "http://")):
        return False
    try:
        return webbrowser.open(url)
    except Exception:
        return False


CONTROLS = {"start-meeting", "stop-meeting", "pause", "resume", "shutdown", "dictate-start", "dictate-stop"}
MAX_TYPED = 4000


def window_state(runtime_dir: Path) -> dict[str, Any] | None:
    state = _read_json(runtime_dir / "window.json")
    return state if state and process_alive(state) else None


class WindowApi:
    """What the page may ask of Python. Each call returns at once; the listener acts on it."""

    def __init__(
        self,
        runtime_dir: Path,
        view: LiveView,
        send_text: Callable[[Path, str], None] = submit,
        send_control: Callable[[Path, str], None] = request,
        announce: Callable[[Path, int], None] = approve_action,
        copy: Callable[[str], bool] = copy_text,
        open_url: Callable[[str], bool] = open_link,
    ) -> None:
        self._runtime_dir = runtime_dir
        self._view = view
        self._send_text = send_text
        self._send_control = send_control
        self._announce = announce
        self._copy = copy
        self._open = open_url
        self._window: Any = None

    def _off(self) -> dict[str, Any] | None:
        if listener_state(self._runtime_dir) is None:
            return {"ok": False, "error": "Buddy is off. Turn it on from your Claude Code session."}
        return None

    def send(self, text: str) -> dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "Nothing to send."}
        if len(text) > MAX_TYPED:
            return {"ok": False, "error": f"Too long: keep it under {MAX_TYPED} characters."}
        refusal = self._off()
        if refusal:
            return refusal
        self._send_text(self._runtime_dir, text)
        return {"ok": True}

    def control(self, action: str) -> dict[str, Any]:
        if action not in CONTROLS:
            return {"ok": False, "error": f"Unknown action: {action}"}
        refusal = self._off()
        if refusal:
            return refusal
        self._send_control(self._runtime_dir, action)
        return {"ok": True}

    def set_mic(self, mode: str) -> dict[str, Any]:
        """The microphone button: follow the meeting app (auto), always keep (on), never (off)."""
        from .mic_gate import MODES, write_mode

        if mode not in MODES:
            return {"ok": False, "error": f"Unknown microphone mode: {mode}"}
        write_mode(self._runtime_dir / "listen" / "mic.json", mode)
        return {"ok": True, "mode": mode}

    def decide_action(self, action_id: int, approve: bool, detail: str | None = None) -> dict[str, Any]:
        """A click on a card. Approve hands it to the session; a copy-only reply is done here."""
        from .actions import ActionStore

        store = ActionStore(self._runtime_dir)
        current = store.get(int(action_id))
        if current is None:
            return {"ok": False, "error": f"No action {action_id}."}
        if approve and current.mode != "copy":
            refusal = self._off()
            if refusal:
                return refusal
        try:
            action = store.decide(int(action_id), bool(approve), None if detail is None else str(detail))
        except (LookupError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        if not approve:
            return {"ok": True, "status": action.status}
        if action.mode == "copy":
            # Nothing here can send it: the reply goes to the clipboard and the thread opens.
            copied = self._copy(action.detail)
            opened = self._open(action.link) if action.link else False
            where = "paste it in the thread" + ("" if opened or not action.link else f": {action.link}")
            result = f"Copied, {where}." if copied else "Could not copy: select the text on the card."
            store.report(action.id, "done" if copied else "failed", result, action.link)
            return {"ok": True, "status": "done" if copied else "failed"}
        self._announce(self._runtime_dir, action.id)
        return {"ok": True, "status": action.status}

    def open_url(self, url: str) -> dict[str, Any]:
        return {"ok": self._open(str(url or ""))}

    def set_show(self, show: str) -> dict[str, Any]:
        if show in {"buddy", "all"}:
            self._view.set_show(show)
        return {"ok": True, "show": self._view.show}

    def set_on_top(self, on_top: bool) -> dict[str, Any]:
        if self._window is not None:
            self._window.on_top = bool(on_top)
        return {"ok": True, "on_top": bool(on_top)}


class Pump:
    """Moves what is new from the view to the page."""

    def __init__(self, view: LiveView, run_js: Callable[[str], Any], status_every: int = 3) -> None:
        self.view = view
        self.run_js = run_js
        self.status_every = status_every
        self._ticks = 0
        self._last_status: dict[str, Any] | None = None

    def step(self) -> bool:
        """One round; True when something was pushed."""
        items = self.view.poll()
        status = None
        if items or self._ticks % self.status_every == 0:
            status = self.view.status()
        self._ticks += 1
        if status == self._last_status:
            status = None  # unchanged: not worth sending again
        if not items and status is None:
            return False
        if status is not None:
            self._last_status = status
        payload = {"items": [item.to_dict() for item in items], "status": status}
        self.run_js(f"window.buddy && window.buddy.update({json.dumps(payload, ensure_ascii=False)})")
        return True


def run_window(runtime_dir: Path, show: str = "buddy", on_top: bool = True, keep: int = 3) -> None:  # pragma: no cover
    """Open the window and block until it is closed. Needs a display and pywebview."""
    import webview

    if window_state(runtime_dir):
        return
    _write_json(runtime_dir / "window.json", {"pid": os.getpid(), "started": _process_started(os.getpid())})
    view = LiveView(runtime_dir, show, keep=keep)
    api = WindowApi(runtime_dir, view)
    width, height = 440, 680
    x = y = None
    if webview.screens:
        screen = webview.screens[0]
        x, y = max(0, screen.width - width - 24), 72
    window = webview.create_window(
        "Buddy", html=PAGE, js_api=api, width=width, height=height, x=x, y=y,
        min_size=(320, 360), on_top=on_top, text_select=True,
    )
    api._window = window
    loaded, closed = threading.Event(), threading.Event()
    window.events.loaded += loaded.set
    window.events.closed += closed.set

    def pump() -> None:
        loaded.wait()
        window.evaluate_js(f"window.buddy && window.buddy.setPinned({json.dumps(on_top)})")
        feeder = Pump(view, window.evaluate_js)
        while not closed.is_set():
            try:
                feeder.step()
            except Exception:  # the page reloading or closing mid-call
                pass
            closed.wait(0.3)

    try:
        webview.start(pump)
    finally:
        if _read_json(runtime_dir / "window.json").get("pid") == os.getpid():
            (runtime_dir / "window.json").unlink(missing_ok=True)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Buddy</title>
<style>
:root {
  --bg: #f7f7f5; --panel: #ffffff; --line: #e4e4df; --text: #1d1d1b; --muted: #6f6f69;
  --accent: #2f6fdf; --accent-soft: #e6eefc; --claude: #b4552d; --claude-soft: #fbefe9;
  --ok: #2e9d57; --rec: #d93a3a; --warn: #b7791f; --tool: #8a8a84; --on-accent: #ffffff;
  --radius: 10px; --font: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
  --mono: "Cascadia Mono", "SF Mono", Consolas, ui-monospace, monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161615; --panel: #1f1f1d; --line: #2e2e2b; --text: #ecece8; --muted: #9b9b95;
    --accent: #7aa7ff; --accent-soft: #1e2a40; --claude: #f0a07a; --claude-soft: #33241c;
    --ok: #4cc27a; --rec: #ff6b6b; --warn: #e0a44a; --tool: #85857f; --on-accent: #0d1626;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { background: var(--bg); color: var(--text); font: 13px/1.45 var(--font); display: flex; flex-direction: column; }
button { font: inherit; color: inherit; background: none; border: 1px solid var(--line); border-radius: 8px;
  padding: 4px 8px; cursor: pointer; }
button:hover { background: var(--accent-soft); }
button.on { background: var(--accent-soft); border-color: var(--accent); }
button.danger.armed { background: var(--rec); color: #fff; border-color: var(--rec); }
/* The microphone button glows while your lines are kept, dims while you are muted in the
   meeting app, and turns amber when no app could be read. */
#mic { display: none; }
#mic.heard { color: #fff; background: var(--rec); border-color: var(--rec); box-shadow: 0 0 10px var(--rec);
  animation: glow 1.6s ease-in-out infinite; }
#mic.muted { opacity: .45; }
#mic.unknown { border-color: #d9a400; color: #d9a400; }
@keyframes glow { 50% { box-shadow: 0 0 3px var(--rec); } }

header { display: flex; align-items: center; gap: 8px; padding: 10px 12px; background: var(--panel);
  border-bottom: 1px solid var(--line); flex-wrap: wrap; }
.dot { width: 10px; height: 10px; border-radius: 50%; background: var(--tool); flex: none; }
.dot.listening { background: var(--ok); }
.dot.meeting { background: var(--rec); animation: pulse 1.6s infinite; }
@keyframes pulse { 50% { opacity: .35; } }
#state { font-weight: 600; }
#brain { color: var(--muted); font-size: 12px; }
.spacer { flex: 1; }
.tools { display: flex; gap: 4px; }

#board { display: none; margin: 10px 12px 0; padding: 8px 10px; background: var(--panel);
  border: 1px solid var(--line); border-radius: var(--radius); }
#board .col { margin-top: 4px; }
#board .col:first-child { margin-top: 0; }
#board .col-title { font-size: 10.5px; letter-spacing: .07em; text-transform: uppercase; color: var(--muted); margin: 2px 0 1px; }
#board .col-now .col-title { color: var(--accent); }
#board .item { display: flex; gap: 6px; align-items: baseline; padding: 2px 0 2px 10px; position: relative; }
#board .item::before { content: "▸"; position: absolute; left: 0; color: var(--muted); font-size: 10px; top: 4px; }
#board .item .t { flex: 1; min-width: 0; }
#board .item .n { color: var(--muted); font-size: 11.5px; }
#board .tk { font: 11px var(--mono); color: var(--accent); border: 1px solid var(--line); border-radius: 6px; padding: 0 5px;
  white-space: nowrap; cursor: pointer; text-decoration: none; }
#board .col-blocked { background: color-mix(in srgb, var(--rec) 10%, transparent); border-radius: 8px; padding: 2px 6px 4px;
  margin-left: -6px; margin-right: -6px; }
#board .col-blocked .col-title, #board .col-blocked .item::before { color: var(--rec); }
#board .col-blocked .why { color: var(--rec); font-size: 12px; display: block; }
#board .col-done .item { color: var(--muted); }
#board .col-done .item::before { content: "✓"; color: var(--ok); }
#board details summary { cursor: pointer; font-size: 10.5px; letter-spacing: .07em; text-transform: uppercase; color: var(--muted); margin-top: 4px; }
#board .compact { display: none; cursor: pointer; font-size: 12px; }
#board .compact b { color: var(--accent); font-weight: 600; }
#board .compact .blk { color: var(--rec); margin-left: 6px; }
#board.small .compact { display: block; }
#board.small .full { display: none; }
#actions { display: none; margin: 10px 12px 0; }
#actions h2, .panel-title { margin: 0 0 6px; font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 8px 10px; margin-bottom: 6px; }
.card .top { display: flex; gap: 6px; align-items: baseline; }
.card .num { color: var(--muted); font: 11px var(--mono); }
.card .kind { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--accent);
  border: 1px solid var(--accent); border-radius: 999px; padding: 0 6px; }
.card .title { font-weight: 600; flex: 1; min-width: 0; }
.card .badge { font-size: 11px; color: var(--warn); }
.card .detail { margin: 6px 0 0; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 9.5em; overflow-y: auto;
  background: var(--bg); border-radius: 8px; padding: 6px 8px; font-size: 12.5px; }
.card textarea.detail { width: 100%; font: inherit; font-size: 12.5px; color: inherit; border: 1px solid var(--accent); resize: vertical; min-height: 6em; }
.card .link { display: block; color: var(--muted); font-size: 11.5px; margin-top: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card .buttons { display: flex; gap: 6px; margin-top: 8px; }
.card .approve { background: var(--accent); color: var(--on-accent); border-color: var(--accent); }
.card .approve.outward { background: var(--claude); border-color: var(--claude); color: #fff; }
.card .state { margin-top: 6px; font-size: 12px; color: var(--muted); }
.card.done { border-color: var(--ok); }
.card.done .state { color: var(--ok); }
.card.failed { border-color: var(--rec); }
.card.failed .state { color: var(--rec); }
.card.finished .detail, .card.finished .buttons { display: none; }
.card.finished { opacity: .8; padding: 6px 10px; }
#suggestions { display: none; margin: 10px 12px 0; padding: 10px 12px; background: var(--accent-soft);
  border: 1px solid var(--accent); border-radius: var(--radius); }
#suggestions h2 { margin: 0 0 4px; font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--accent); }
#suggestions ol { margin: 0; padding: 0; list-style: none; }
#suggestions ol { max-height: 34vh; overflow-y: auto; }
#suggestions li { padding: 3px 0; }
#suggestions li.older { opacity: .55; }
#suggestions li.set-start { border-top: 1px dashed var(--accent); margin-top: 4px; padding-top: 6px; }

#activity-head { display: flex; align-items: center; gap: 6px; margin: 8px 12px 0; padding: 0; border: 0;
  color: var(--muted); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; background: none; }
#activity-head:hover { background: none; color: var(--text); }
#activity-head .chev { transition: transform .15s; }
body.activity-closed #activity-head .chev { transform: rotate(-90deg); }
body.activity-closed #feed, body.activity-closed #jump { display: none !important; }
#panels { overflow-y: auto; max-height: 58vh; flex: none; }
body.activity-closed #panels { flex: 1; max-height: none; }
#feed { flex: 1; overflow-y: auto; padding: 8px 12px 12px; }
.row { display: flex; gap: 8px; padding: 3px 0; }
.time { color: var(--muted); font: 11px var(--mono); padding-top: 2px; flex: none; width: 56px; }
.body { flex: 1; min-width: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.who { font-weight: 600; margin-right: 6px; }
.said-ME .who { color: var(--accent); }
.said-REMOTE .who { color: var(--muted); }
.dictation .body { color: var(--accent); font-style: italic; }
.command .body { font-weight: 600; }
.note { color: var(--warn); font-weight: 400; margin-left: 6px; }
.state .body, .cancelled .body, .notice .body { color: var(--muted); font-size: 12px; }
.claude .body { background: var(--claude-soft); border-radius: var(--radius); padding: 8px 10px; }
.claude .who { color: var(--claude); display: block; margin-bottom: 2px; }
.tool .body { color: var(--tool); font: 12px var(--mono); }
.you .who { color: var(--muted); }
#empty { color: var(--muted); text-align: center; padding: 40px 16px; }

#jump { display: none; position: absolute; left: 50%; transform: translateX(-50%); bottom: 76px;
  background: var(--accent); color: var(--on-accent); border: 0; border-radius: 999px; padding: 4px 12px; }
footer { padding: 10px 12px; background: var(--panel); border-top: 1px solid var(--line); }
#form { display: flex; gap: 8px; align-items: flex-end; }
#input { flex: 1; resize: none; font: inherit; color: inherit; background: var(--bg); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 7px 10px; max-height: 120px; }
#input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
#send { background: var(--accent); color: var(--on-accent); border-color: var(--accent); padding: 7px 14px; }
/* The 🎤 dictation button glows while Buddy is listening to you. */
#dictate { padding: 7px 10px; }
#dictate.live { color: #fff; background: var(--rec); border-color: var(--rec); box-shadow: 0 0 10px var(--rec);
  animation: glow 1.6s ease-in-out infinite; }
#dictate.finishing { opacity: .6; }
#input.listening { outline: 2px solid var(--rec); outline-offset: -1px; }
#error { color: var(--rec); font-size: 12px; min-height: 0; }
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <span id="state">Connecting…</span>
  <span id="brain"></span>
  <span class="spacer"></span>
  <span class="tools">
    <button id="mic" title="Your microphone">🎙</button>
    <button id="meeting" title="Start or end recording a meeting">⏺ Meeting</button>
    <button id="pause" title="Close or reopen the microphone">⏸</button>
    <button id="show" title="Show only what Buddy started, or the whole Claude session">Only Buddy</button>
    <button id="pin" class="on" title="Keep this window above the others">📌</button>
    <button id="power" class="danger" title="Turn Buddy off">⏻</button>
  </span>
</header>
<div id="panels">
  <section id="actions"><h2 id="actions-title">Actions</h2><div id="action-list"></div></section>
  <section id="board" title="Your work: now, next, blocked"><div class="compact" id="board-compact"></div><div class="full" id="board-full"></div></section>
</div>
<section id="suggestions"><h2>Suggestions</h2><ol id="suggestion-list"></ol></section>
<button id="activity-head" title="Show or hide what Buddy and Claude are doing"><span class="chev">▾</span> Activity</button>
<main id="feed"><div id="empty">Type below, or press 🎤 to talk to Buddy.<br>The meeting shows here as it is said.</div></main>
<button id="jump">↓ New</button>
<footer>
  <form id="form">
    <textarea id="input" rows="1" placeholder="Type to Buddy — e.g. review PR 123"></textarea>
    <button id="dictate" type="button" title="Talk to Buddy: click, speak, click again. The text lands here to read and send.">🎤</button>
    <button id="send" type="submit">Send</button>
  </form>
  <div id="error"></div>
</footer>
<script>
(() => {
  const $ = (id) => document.getElementById(id);
  const feed = $("feed"), input = $("input"), errorBox = $("error");
  const MAX_ROWS = 600;
  let status = {listener: "off", brain: "none", suggestions: [], show: "buddy"};
  let pinned = true;

  const api = () => (window.pywebview && window.pywebview.api) || null;
  const call = async (name, ...args) => {
    const a = api();
    if (!a) return {ok: false, error: "Still starting…"};
    try { return await a[name](...args); } catch (e) { return {ok: false, error: String(e)}; }
  };
  const showError = (message) => {
    errorBox.textContent = message || "";
    if (message) setTimeout(() => { if (errorBox.textContent === message) errorBox.textContent = ""; }, 6000);
  };

  const nearBottom = () => feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
  feed.addEventListener("scroll", () => { if (nearBottom()) $("jump").style.display = "none"; });
  $("jump").addEventListener("click", () => { feed.scrollTop = feed.scrollHeight; $("jump").style.display = "none"; });

  const LABELS = {dictation: "ME ▸", claude: "Claude", tool: "⚙", you: "You, in the chat"};

  function row(item) {
    const el = document.createElement("div");
    el.className = "row " + item.kind + (item.kind === "said" ? " said-" + item.who : "");
    const time = document.createElement("span");
    time.className = "time";
    time.textContent = item.clock || "";
    const body = document.createElement("div");
    body.className = "body";
    let label = LABELS[item.kind] || "";
    if (item.kind === "said") label = item.who;
    if (item.kind === "command") label = item.who === "typed" ? "Sent ➜" : "Sent by voice ➜";
    if (label) {
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = label;
      body.appendChild(who);
    }
    body.appendChild(document.createTextNode(item.text));
    if (item.note) {
      const note = document.createElement("span");
      note.className = "note";
      note.textContent = "(" + item.note + ")";
      body.appendChild(note);
    }
    el.append(time, body);
    return el;
  }

  function addItems(items) {
    if (!items.length) return;
    const empty = $("empty");
    if (empty) empty.remove();
    for (const item of items) feed.appendChild(row(item));
    while (feed.children.length > MAX_ROWS) feed.firstChild.remove();
  }

  function minutesSince(meetingId) {
    const m = /^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(\d{2})/.exec(meetingId || "");
    if (!m) return null;
    const start = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
    return Math.max(0, Math.round((Date.now() - start) / 60000));
  }

  function setStatus(next) {
    status = next;
    const state = next.listener;
    $("dot").className = "dot " + state;
    const ready = state === "paused" && next.wake_phrase === false;  // waiting for a meeting or the 🎤
    let label = ready ? "Ready" : ({listening: "Listening", meeting: "Recording", paused: "Paused", off: "Buddy is off"}[state] || state);
    if (state === "meeting") {
      const minutes = minutesSince(next.meeting);
      if (minutes !== null) label += " · " + minutes + " min";
    }
    $("state").textContent = label;
    $("brain").textContent = {
      watching: "· Claude connected",
      idle: "· Claude's monitor is off",
      none: "· no Claude session yet",
    }[next.brain] || "";
    const on = state !== "off";
    $("meeting").textContent = state === "meeting" ? "⏹ End meeting" : "⏺ Meeting";
    $("meeting").disabled = !on;
    $("pause").textContent = state === "paused" ? "▶" : "⏸";
    $("pause").disabled = !on;
    $("pause").style.display = ready ? "none" : "";
    $("power").disabled = !on;
    $("show").textContent = next.show === "all" ? "All turns" : "Only Buddy";
    const mic = next.mic;
    const micButton = $("mic");
    micButton.style.display = mic && state === "meeting" ? "inline-block" : "none";
    if (mic) {
      const look = mic.mode !== "auto" ? (mic.heard ? "heard" : "muted")
        : mic.state === "none" ? "unknown" : (mic.heard ? "heard" : "muted");
      micButton.className = look;
      micButton.textContent = mic.mode === "auto" ? "🎙" : (mic.mode === "on" ? "🎙 on" : "🎙 off");
      const app = mic.app || "no meeting app read";
      micButton.title = (mic.heard ? "Your lines are kept" : "Your lines are not kept") +
        ` · ${mic.mode === "auto" ? "following " + app : "set by hand"} · click: auto → on → off`;
    }
    showDictation(next.dictation, on);
    showActions(next.actions);
    showBoard(next.board, state === "meeting");
    // Earlier sets stay above the newest one, dimmed, so a new set never wipes the one
    // being read. The newest set is at the bottom and kept in view.
    const list = $("suggestion-list");
    const all = next.suggestions || [];
    const firstNew = all.length - (next.suggestions_latest || all.length);
    list.replaceChildren(...all.map((text, i) => {
      const li = document.createElement("li");
      li.textContent = text;
      if (i < firstNew) li.classList.add("older");
      if (i === firstNew && i > 0) li.classList.add("set-start");
      return li;
    }));
    list.scrollTop = list.scrollHeight;
    $("suggestions").style.display = all.length ? "block" : "none";
  }

  // 🎤: while on, the box shows what is heard; when done, the text stays there to read,
  // fix and send. `id` grows once per finished dictation, so each lands only once.
  let dictationSeen = null, dictationBase = "", dictating = false, finishing = false;
  function showDictation(d, on) {
    const button = $("dictate");
    button.disabled = !on;
    if (!d) return;
    if (dictationSeen === null) dictationSeen = d.id;  // the window opened after it: nothing to land
    if (d.active) {
      if (!dictating) { dictating = true; dictationBase = input.value.trim(); }
      input.value = (dictationBase + " " + d.text).trim();
      resize();
    } else if (d.id !== dictationSeen) {
      dictationSeen = d.id;
      input.value = ((dictating ? dictationBase : input.value.trim()) + " " + d.text).trim();
      resize();
      dictating = finishing = false;
      input.focus();
    } else if (dictating) {
      dictating = finishing = false;
    }
    button.className = d.active ? (finishing ? "finishing" : "live") : "";
    button.title = d.active ? "Listening… click to finish" : "Talk to Buddy: click, speak, click again. The text lands here to read and send.";
    input.classList.toggle("listening", !!d.active);
    input.placeholder = d.active ? "Listening…" : "Type to Buddy — e.g. review PR 123";
  }

  // Action cards. Rebuilt only when an action changes, and never while you edit it.
  const KIND = {reply: "Reply", review: "Review", investigate: "Look into", merge: "Merge", post: "Post", ticket: "Jira", run: "Run", other: "Action"};
  const cards = new Map(), editing = new Set();
  function card(a) {
    const el = document.createElement("div");
    const finished = ["done", "failed"].includes(a.status);
    el.className = "card " + a.status + (finished ? " finished" : "");
    const top = document.createElement("div");
    top.className = "top";
    const num = document.createElement("span"); num.className = "num"; num.textContent = "#" + a.id;
    const kind = document.createElement("span"); kind.className = "kind"; kind.textContent = KIND[a.kind] || a.kind;
    const title = document.createElement("span"); title.className = "title"; title.textContent = a.title;
    top.append(num, kind, title);
    if (a.stale) { const b = document.createElement("span"); b.className = "badge"; b.textContent = "from yesterday"; top.append(b); }
    el.append(top);
    let detailEl = null;
    if (a.detail) {
      detailEl = document.createElement("div"); detailEl.className = "detail"; detailEl.textContent = a.detail;
      el.append(detailEl);
    }
    if (a.link) {
      const link = document.createElement("a"); link.className = "link"; link.href = a.link; link.textContent = a.link;
      link.addEventListener("click", (e) => { e.preventDefault(); call("open_url", a.link); });
      el.append(link);
    }
    if (a.status === "proposed") {
      const buttons = document.createElement("div"); buttons.className = "buttons";
      const approve = document.createElement("button");
      approve.className = "approve" + (a.outward && a.mode !== "copy" ? " outward" : "");
      approve.textContent = a.mode === "copy" ? "Copy & open" : (a.outward ? "Approve & send" : "Approve");
      approve.title = a.outward ? "Leaves this machine exactly as shown" : "Claude does this now";
      const edit = document.createElement("button"); edit.textContent = "Edit";
      const dismiss = document.createElement("button"); dismiss.textContent = "Dismiss";
      if (!a.detail) edit.style.display = "none";
      const decide = async (yes) => {
        approve.disabled = edit.disabled = dismiss.disabled = true;  // one click, one run
        const area = el.querySelector("textarea.detail");
        const r = await call("decide_action", a.id, yes, area ? area.value : null);
        editing.delete(a.id);
        if (!r.ok) { showError(r.error); approve.disabled = edit.disabled = dismiss.disabled = false; }
      };
      approve.addEventListener("click", () => decide(true));
      dismiss.addEventListener("click", () => decide(false));
      edit.addEventListener("click", () => {
        if (el.querySelector("textarea.detail")) return;
        editing.add(a.id);
        const area = document.createElement("textarea"); area.className = "detail"; area.value = a.detail;
        detailEl.replaceWith(area); area.focus();
        edit.textContent = "Editing…"; edit.disabled = true;
      });
      buttons.append(approve, edit, dismiss);
      el.append(buttons);
    } else {
      const state = document.createElement("div"); state.className = "state";
      const waiting = status.brain === "watching" ? "Claude is on it…" : "Approved, waiting for Claude to pick it up…";
      state.textContent = {approved: waiting, running: "Claude is on it…", done: "✓ " + (a.result || "Done"),
        failed: "✗ " + (a.result || "Failed")}[a.status] || a.status;
      if (a.result_link && finished) {
        const link = document.createElement("a"); link.className = "link"; link.href = a.result_link; link.textContent = a.result_link;
        link.addEventListener("click", (e) => { e.preventDefault(); call("open_url", a.result_link); });
        state.append(link);
      }
      el.append(state);
    }
    return el;
  }
  function showActions(actions) {
    const shown = (actions || []).filter((a) => a.status !== "dismissed");
    const open = shown.filter((a) => !["done", "failed"].includes(a.status));
    const finished = shown.filter((a) => ["done", "failed"].includes(a.status)).slice(-3);
    const list = $("action-list");
    const wanted = [...finished, ...open];
    const keep = new Set(wanted.map((a) => a.id));
    for (const [id, entry] of cards) if (!keep.has(id)) { entry.el.remove(); cards.delete(id); editing.delete(id); }
    for (const a of wanted) {
      const key = JSON.stringify(a) + (a.status === "approved" ? status.brain : "");
      const entry = cards.get(a.id);
      if (entry && (entry.key === key || editing.has(a.id))) { list.appendChild(entry.el); continue; }
      const el = card(a);
      if (entry) entry.el.replaceWith(el);
      cards.set(a.id, {el, key});
      list.appendChild(el);
    }
    $("actions-title").textContent = open.length ? `Actions · ${open.filter((a) => a.status === "proposed").length} to approve` : "Actions";
    $("actions").style.display = wanted.length ? "block" : "none";
  }

  // The work board. Small during a meeting (one line: what is being done now); a click opens it.
  let boardOpenInMeeting = false;
  $("board-compact").addEventListener("click", () => { boardOpenInMeeting = true; setStatus(status); });
  function ticketChip(item) {
    const chip = document.createElement(item.link ? "a" : "span");
    chip.className = "tk";
    chip.textContent = item.ticket + (item.ticket_status ? " · " + item.ticket_status : "");
    if (item.ticket_assignee) chip.title = "Assigned to " + item.ticket_assignee;
    if (item.link) { chip.href = item.link; chip.addEventListener("click", (e) => { e.preventDefault(); call("open_url", item.link); }); }
    return chip;
  }
  function boardItem(item, withWhy) {
    const row = document.createElement("div"); row.className = "item";
    const t = document.createElement("span"); t.className = "t"; t.textContent = item.title;
    if (withWhy && item.blocker) { const why = document.createElement("span"); why.className = "why"; why.textContent = item.blocker; t.append(why); }
    else if (item.note) { const n = document.createElement("span"); n.className = "n"; n.textContent = " — " + item.note; t.append(n); }
    if (!item.ticket && item.link) {
      const chip = document.createElement("a"); chip.className = "tk"; chip.textContent = "link"; chip.href = item.link;
      chip.addEventListener("click", (e) => { e.preventDefault(); call("open_url", item.link); });
      row.append(t, chip);
    } else row.append(t, ...(item.ticket ? [ticketChip(item)] : []));
    return row;
  }
  function showBoard(board, inMeeting) {
    const b = board || {};
    const cols = [["now", "Now"], ["blocked", "Blocked"], ["next", "Next"]];
    const full = $("board-full");
    full.replaceChildren();
    for (const [key, name] of cols) {
      const items = b[key] || [];
      if (!items.length) continue;
      const col = document.createElement("div"); col.className = "col col-" + key;
      const title = document.createElement("div"); title.className = "col-title"; title.textContent = name;
      col.append(title, ...items.map((i) => boardItem(i, key === "blocked")));
      full.append(col);
    }
    const done = b.done || [];
    if (done.length) {
      const det = document.createElement("details"); det.className = "col col-done";
      const sum = document.createElement("summary"); sum.textContent = `Done today (${done.length})`;
      det.append(sum, ...done.map((i) => boardItem(i, false)));
      full.append(det);
    }
    const now = b.now || [], blocked = b.blocked || [];
    const compact = $("board-compact");
    compact.replaceChildren();
    const lead = document.createElement("b"); lead.textContent = "Now: ";
    compact.append(lead, document.createTextNode(now.map((i) => i.title).join(" · ") || "—"));
    if (blocked.length) { const k = document.createElement("span"); k.className = "blk"; k.textContent = `● ${blocked.length} blocked`; compact.append(k); }
    if (!inMeeting) boardOpenInMeeting = false;
    $("board").classList.toggle("small", inMeeting && !boardOpenInMeeting);
    $("board").style.display = full.children.length ? "block" : "none";
  }

  window.buddy = {
    update(payload) {
      // Decided before anything moves: the suggestions box resizing the feed must not
      // leave the latest line out of view.
      const stick = nearBottom();
      const items = payload.items || [];
      if (payload.status) setStatus(payload.status);
      addItems(items);
      if (stick) feed.scrollTop = feed.scrollHeight;
      else if (items.length) $("jump").style.display = "block";
    },
    setPinned(value) {
      pinned = !!value;
      $("pin").classList.toggle("on", pinned);
    },
  };

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    const result = await call("send", text);
    if (result.ok) { input.value = ""; resize(); showError(""); }
    else showError(result.error);
  }
  const resize = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 120) + "px"; };
  input.addEventListener("input", resize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $("form").addEventListener("submit", (e) => { e.preventDefault(); send(); });

  const control = async (action) => { const r = await call("control", action); if (!r.ok) showError(r.error); };
  $("meeting").addEventListener("click", () => control(status.listener === "meeting" ? "stop-meeting" : "start-meeting"));
  $("pause").addEventListener("click", () => control(status.listener === "paused" ? "resume" : "pause"));
  $("dictate").addEventListener("click", async () => {
    const live = status.dictation && status.dictation.active;
    if (live) { finishing = true; $("dictate").className = "finishing"; }
    const r = await call("control", live ? "dictate-stop" : "dictate-start");
    if (!r.ok) showError(r.error);
  });
  const ACTIVITY_KEY = "buddy.activity";
  const setActivity = (open) => {
    document.body.classList.toggle("activity-closed", !open);
    try { localStorage.setItem(ACTIVITY_KEY, open ? "open" : "closed"); } catch (e) {}
  };
  try { if (localStorage.getItem(ACTIVITY_KEY) === "closed") document.body.classList.add("activity-closed"); } catch (e) {}
  $("activity-head").addEventListener("click", () => setActivity(document.body.classList.contains("activity-closed")));
  $("mic").addEventListener("click", async () => {
    const order = ["auto", "on", "off"];
    const current = (status.mic && status.mic.mode) || "auto";
    const r = await call("set_mic", order[(order.indexOf(current) + 1) % order.length]);
    if (!r.ok) showError(r.error);
  });
  $("show").addEventListener("click", async () => {
    const r = await call("set_show", status.show === "all" ? "buddy" : "all");
    if (r.ok) { status.show = r.show; $("show").textContent = r.show === "all" ? "All turns" : "Only Buddy"; }
  });
  $("pin").addEventListener("click", async () => {
    pinned = !pinned;
    await call("set_on_top", pinned);
    $("pin").classList.toggle("on", pinned);
  });
  let armedAt = 0;
  $("power").addEventListener("click", () => {
    // Two clicks within four seconds: turning Buddy off by accident mid-meeting is costly.
    if (Date.now() - armedAt < 4000) { armedAt = 0; $("power").classList.remove("armed"); control("shutdown"); return; }
    armedAt = Date.now();
    $("power").classList.add("armed");
    $("power").title = "Click again to turn Buddy off";
    setTimeout(() => { $("power").classList.remove("armed"); $("power").title = "Turn Buddy off"; }, 4000);
  });
  window.addEventListener("pywebviewready", () => input.focus());
})();
</script>
</body>
</html>
"""
