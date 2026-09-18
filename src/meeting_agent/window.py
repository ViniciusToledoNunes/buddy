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

from .listener import _process_started, _read_json, _write_json, listener_state, process_alive, request, submit
from .live import LiveView

CONTROLS = {"start-meeting", "stop-meeting", "pause", "resume", "shutdown"}
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
    ) -> None:
        self._runtime_dir = runtime_dir
        self._view = view
        self._send_text = send_text
        self._send_control = send_control
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


def run_window(runtime_dir: Path, show: str = "buddy", on_top: bool = True) -> None:  # pragma: no cover
    """Open the window and block until it is closed. Needs a display and pywebview."""
    import webview

    if window_state(runtime_dir):
        return
    _write_json(runtime_dir / "window.json", {"pid": os.getpid(), "started": _process_started(os.getpid())})
    view = LiveView(runtime_dir, show)
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

#suggestions { display: none; margin: 10px 12px 0; padding: 10px 12px; background: var(--accent-soft);
  border: 1px solid var(--accent); border-radius: var(--radius); }
#suggestions h2 { margin: 0 0 4px; font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--accent); }
#suggestions ol { margin: 0; padding: 0; list-style: none; }
#suggestions li { padding: 3px 0; }

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
    <button id="meeting" title="Start or end recording a meeting">⏺ Meeting</button>
    <button id="pause" title="Close or reopen the microphone">⏸</button>
    <button id="show" title="Show only what Buddy started, or the whole Claude session">Only Buddy</button>
    <button id="pin" class="on" title="Keep this window above the others">📌</button>
    <button id="power" class="danger" title="Turn Buddy off">⏻</button>
  </span>
</header>
<section id="suggestions"><h2>Suggestions</h2><ol id="suggestion-list"></ol></section>
<main id="feed"><div id="empty">Say “Hey Buddy”, or type below.<br>The meeting shows here as it is said.</div></main>
<button id="jump">↓ New</button>
<footer>
  <form id="form">
    <textarea id="input" rows="1" placeholder="Type to Buddy — e.g. review PR 123"></textarea>
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
    let label = {listening: "Listening", meeting: "Recording", paused: "Paused", off: "Buddy is off"}[state] || state;
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
    $("power").disabled = !on;
    $("show").textContent = next.show === "all" ? "All turns" : "Only Buddy";
    const list = $("suggestion-list");
    list.replaceChildren(...(next.suggestions || []).map((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      return li;
    }));
    $("suggestions").style.display = (next.suggestions || []).length ? "block" : "none";
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
