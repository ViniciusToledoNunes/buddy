from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .asr import select_asr
from .audio import list_audio_devices
from .benchmark import run_benchmark
from .config import config_path, load_settings, project_root
from .doctor import run_doctor
from .session import STATE_FILE, _hotkey, _state_process_alive, read_state, request_stop, run_session


app = typer.Typer(no_args_is_help=True, help="Buddy - your cross-platform, project-aware meeting copilot")
console = Console()


@app.command()
def doctor() -> None:
    """Inspect hardware, ASR, FFmpeg, APIs, and the platform audio backend."""
    table = Table("Check", "State", "Detail")
    colors = {"ok": "green", "warning": "yellow", "failed": "red", "not-needed": "dim"}
    for check in run_doctor(load_settings()):
        table.add_row(check.name, f"[{colors.get(check.state, 'white')}]{check.state}[/{colors.get(check.state, 'white')}]", check.detail)
    console.print(table)


@app.command()
def devices() -> None:
    """List system-output and microphone endpoints for the active platform."""
    data = list_audio_devices()
    console.print(f"Default output: [bold]{data['default_speaker']}[/bold]")
    console.print(f"Default microphone: [bold]{data['default_microphone']}[/bold]")
    console.print(f"Backend: [bold]{data.get('backend', 'unknown')}[/bold]")
    table = Table("Type", "Name", "Device id")
    for item in data["speakers"]:
        table.add_row("OUTPUT", item["name"], item["id"])
    for item in data["microphones"]:
        table.add_row("LOOPBACK" if item["loopback"] else "MIC", item["name"], item["id"])
    console.print(table)


@app.command()
def benchmark(
    model: list[str] = typer.Option(None, "--model", "-m", help="Model(s) to test; defaults to config list"),
) -> None:
    """Benchmark local ASR against the first configured 60 seconds; never uploads audio."""
    settings = load_settings()
    console.print("Local-only benchmark. Audio will not be sent to any external API.")
    report = run_benchmark(settings, model or None)
    table = Table("Backend", "Model", "Device", "Compute", "Audio", "Process", "RTF", "3s latency", "RAM peak", "VRAM")
    for item in report["results"]:
        if item["status"] != "ok":
            table.add_row(item["backend"], item["model"], item["device"], item["compute_type"], "-", "FAILED", "-", item.get("error", ""), "-", "-")
            continue
        table.add_row(
            item["backend"], item["model"], item["device"], item["compute_type"],
            f"{item['audio_seconds']:.1f}s", f"{item['processing_seconds']:.2f}s", f"{item['real_time_factor']:.3f}",
            f"{item['approximate_3s_latency_seconds']:.2f}s", f"{item['process_ram_peak_mb']:.0f} MB", "N/A",
        )
    console.print(table)
    console.print(f"Recommended: [bold green]{report['recommended']}[/bold green]")
    console.print(f"Saved: {project_root() / 'benchmark-results.json'}")


@app.command()
def start() -> None:
    """Start an explicit foreground meeting session."""
    settings = load_settings()
    selection = select_asr(settings)
    console.print("[bold red]BUDDY - RECORDING / TRANSCRIBING[/bold red]")
    console.print(f"ASR: {selection.mode} / {selection.model} / {selection.compute_type} ({selection.reason})")
    console.print(f"Stop: buddy stop or {settings.hotkeys.toggle_meeting}; suggest: {settings.hotkeys.suggest_now}")
    try:
        directory = asyncio.run(run_session(settings))
        console.print(f"Meeting saved to [bold]{directory}[/bold]")
    except KeyboardInterrupt:
        request_stop()
        console.print("Stopping...")
    except Exception as exc:
        console.print(f"[red]Cannot start:[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(1)


@app.command()
def stop() -> None:
    """Request immediate capture stop from another terminal."""
    if request_stop():
        console.print("Stop requested. Capture will end immediately and the report will be generated.")
    else:
        console.print("No active meeting session.")


@app.command()
def status() -> None:
    """Show whether Buddy is listening, recording a meeting, or stopped."""
    from .listener import listener_state

    listening = listener_state()
    if listening:
        console.print_json(json.dumps({"listener": listening}))
        return
    state = read_state()
    if not state or not _state_process_alive(state):
        console.print("[dim]STOPPED - not listening and no capture is active.[/dim]")
        return
    console.print_json(json.dumps(state))


@app.command("config")
def config_command(path_only: bool = typer.Option(False, "--path", help="Print only the config path")) -> None:
    """Show the active configuration and selected backend."""
    path = config_path()
    if path_only:
        console.print(str(path))
        return
    console.print(f"Config: [bold]{path}[/bold]")
    console.print(path.read_text(encoding="utf-8"))
    try:
        selection = select_asr(load_settings(path))
        console.print(f"Resolved ASR: {selection.mode} / {selection.model} / {selection.compute_type}")
    except Exception as exc:
        console.print(f"[red]Selection error:[/red] {exc}")


@app.command()
def hotkeys() -> None:
    """Run a small listener so the configured toggle hotkey can start/stop sessions."""
    settings = load_settings()
    child: subprocess.Popen[bytes] | None = None

    def toggle() -> None:
        nonlocal child
        if STATE_FILE.exists() and request_stop():
            return
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        child = subprocess.Popen([sys.executable, "-m", "meeting_agent.cli", "start"], creationflags=flags)

    console.print(f"Hotkey listener active: {settings.hotkeys.toggle_meeting} starts/stops. Ctrl+C exits listener.")
    try:
        from pynput import keyboard
    except Exception as exc:  # a headless host has no global hotkey backend
        console.print(f"[red]Hotkeys unavailable on this host: {exc}[/red]")
        raise typer.Exit(code=1)
    listener = keyboard.GlobalHotKeys({_hotkey(settings.hotkeys.toggle_meeting): toggle})
    listener.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        listener.stop()


# Connector tools only: the brain has its own file and search tools, so exposing the
# project index here would just search Buddy's repository again.
CONNECTOR_PREFIXES = ("bigquery_", "datadog_", "jira_", "search_past_meetings")


@app.command()
def tool(
    name: str = typer.Argument("", help="Tool to run, as shown by --list."),
    arguments: str = typer.Argument("{}", help="JSON object with the tool's arguments."),
    list_tools: bool = typer.Option(False, "--list", help="List the available read-only tools."),
) -> None:
    """Run one read-only connector (BigQuery, Datadog, Jira) and print its result.

    This is how the Claude Code brain reaches the guarded connectors: they refuse writes
    and oversized queries in code, whatever the caller asks for.
    """
    from .investigator import Investigator

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = load_settings()
    meetings = Path(settings.meetings_dir)
    if not meetings.is_absolute():
        meetings = project_root() / meetings
    registry = Investigator(settings, project_root(), meetings).tools
    schemas = [s for s in registry.schemas if s["name"].startswith(CONNECTOR_PREFIXES)]
    if list_tools or not name:
        for schema in schemas:
            properties = schema.get("parameters", {}).get("properties", {})
            args = ", ".join(f"{key}: {value.get('type', '?')}" for key, value in properties.items())
            print(f"{schema['name']}({args}) - {schema['description']}")
        return
    if name not in {s["name"] for s in schemas}:
        print(f"Unknown tool {name!r}. Run with --list.")
        raise typer.Exit(code=2)
    try:
        parsed = json.loads(arguments or "{}")
    except ValueError as exc:
        print(f"Arguments must be a JSON object: {exc}")
        raise typer.Exit(code=2)
    if not isinstance(parsed, dict):
        print("Arguments must be a JSON object.")
        raise typer.Exit(code=2)
    print(registry.call(name, parsed))


# ----------------------------------------------------------------- listening


def _runtime_dir() -> Path:
    return project_root() / ".meeting-agent"


def _require_listener() -> None:
    from .listener import listener_state

    if not listener_state(_runtime_dir()):
        console.print("[red]Buddy is not listening. Start it with `buddy listen --detach`.[/red]")
        raise typer.Exit(code=1)


@app.command()
def listen(
    detach: bool = typer.Option(False, "--detach", help="Run in the background and return."),
    stop_listener: bool = typer.Option(False, "--stop", help="Shut down a running listener."),
) -> None:
    """Keep the microphone attentive for "Hey Buddy"; record meetings when asked.

    Speech is kept only when it opens with the wake phrase or a meeting is being
    recorded. Events go to the log that `buddy watch` follows.
    """
    from .listener import ListenDaemon, listener_state, request

    runtime = _runtime_dir()
    if stop_listener:
        _require_listener()
        request(runtime, "shutdown")
        for _ in range(60):
            if not listener_state(runtime):
                console.print("Buddy stopped listening.")
                return
            time.sleep(0.5)
        console.print("[yellow]Shutdown requested; the listener has not exited yet.[/yellow]")
        return
    if detach:
        if listener_state(runtime):
            console.print("Buddy is already listening.")
            return
        runtime.mkdir(parents=True, exist_ok=True)
        log = (runtime / "listen.log").open("a", encoding="utf-8")
        kwargs: dict = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "cwd": str(project_root())}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "meeting_agent.cli", "listen"], **kwargs)
        for _ in range(60):
            state = listener_state(runtime)
            if state:
                settings = load_settings()
                ready = "listening for \"Hey Buddy\"" if settings.listen.wake_phrase else "ready; the microphone opens for meetings and the 🎤"
                console.print(f"Buddy is {ready} (pid {state['pid']}).")
                if settings.listen.window:
                    _open_window(runtime)
                return
            time.sleep(0.5)
        console.print(f"[red]The listener did not start; see {runtime / 'listen.log'}.[/red]")
        raise typer.Exit(code=1)
    settings = load_settings()
    daemon = ListenDaemon(settings, runtime)
    console.print('Listening for "Hey Buddy". Ctrl+C stops.' if settings.listen.wake_phrase
                  else "Ready: the microphone opens for meetings and the window's 🎤. Ctrl+C stops.")
    try:
        asyncio.run(daemon.run(asyncio.Event()))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)


@app.command()
def watch(
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep running and print events as they happen."),
    peek: bool = typer.Option(False, "--peek", help="Show new events without advancing the cursor."),
    consumer: str = typer.Option("default", "--as", help="Cursor name; each watcher gets its own."),
    as_json: bool = typer.Option(False, "--json", help="Print raw JSON records."),
    interval: float = typer.Option(1.0, "--interval", help="Seconds between checks with --follow."),
) -> None:
    """Print what the listener noticed since this watcher last looked.

    One line per event, so `--follow` can drive a Claude Code Monitor. It reports when
    the listener goes down and then exits: a silent monitor must not look like a quiet
    room, and a monitor must not outlive the listener it follows.
    """
    from .listener import EventReader, format_event, listener_state
    from .live import register_brain, unregister_brain

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    runtime = _runtime_dir()
    safe = "".join(ch for ch in consumer if ch.isalnum() or ch in "-_") or "default"
    reader = EventReader(runtime / "events.jsonl", runtime / "cursors" / f"{safe}.json")
    cursor = reader.load_cursor()
    settings = load_settings()
    last_type = ""
    # Run by a Claude Code session, the watch knows which one it is: Buddy's window
    # mirrors that session's own record to show what it does with each event.
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if follow and not peek and session_id:
        register_brain(runtime, session_id, safe)

    def drain() -> None:
        nonlocal cursor, last_type
        records, cursor = reader.read(cursor)
        for record in records:
            last_type = str(record.get("type", ""))
            line = json.dumps(record, ensure_ascii=False) if as_json else format_event(
                record, settings.listen.event_line_max_chars
            )
            print(line, flush=True)
        if not peek:
            reader.save_cursor(cursor)

    try:
        while True:
            drain()
            if listener_state(runtime) is None:
                # The listener writes LISTENER_DOWN just before it lets go of its state, so one
                # more read catches it; a crash leaves no such line, and one is printed here.
                # Exiting ends a Monitor, so nothing keeps watching a listener that is gone.
                drain()
                if last_type != "LISTENER_DOWN":
                    print("LISTENER_DOWN Buddy is not listening; start it with `buddy listen --detach`.", flush=True)
                return
            if not follow:
                return
            time.sleep(max(0.2, interval))
    finally:
        if follow and not peek and session_id:
            unregister_brain(runtime)


@app.command()
def say(text: list[str] = typer.Argument(..., help="The command, as you would say it after \"Hey Buddy\".")) -> None:
    """Send Buddy a typed command. It reaches the session exactly like a spoken one."""
    from .listener import submit

    _require_listener()
    submit(_runtime_dir(), " ".join(text))
    console.print("Sent.")


@app.command()
def propose(
    title: str = typer.Argument(..., help="What would be done, in a few words: \"Reply to Nick (DM)\"."),
    kind: str = typer.Option("other", "--kind", "-k", help="reply, review, investigate, merge, post, run or other."),
    detail: str = typer.Option("", "--detail", "-d", help="Exactly what will be sent or done."),
    detail_file: Path = typer.Option(None, "--detail-file", help="Read the detail from this file (- for stdin)."),
    link: str = typer.Option("", "--link", "-l", help="The thread, PR or ticket it is about."),
    mode: str = typer.Option("run", "--mode", "-m", help="run: the session does it; copy: copy and open the link; send: the session sends it."),
    outward: bool = typer.Option(None, "--outward/--local", help="Whether it leaves this machine (default: by kind)."),
    source: str = typer.Option("session", "--source", help="Where it came from: slack, meeting, session."),
) -> None:
    """Put an action on Buddy's window for the user to approve with one click."""
    from .actions import ActionStore

    if detail_file is not None:
        detail = sys.stdin.read() if str(detail_file) == "-" else detail_file.read_text(encoding="utf-8")
    try:
        action = ActionStore(_runtime_dir()).propose(kind, title, detail, link, mode, outward, source)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    console.print(f"Proposed action {action.id}: {action.title}")


@app.command("action")
def action_command(
    action_id: int = typer.Argument(..., help="The action's number."),
    status: str = typer.Argument(..., help="running, done or failed; or show."),
    result: str = typer.Option("", "--result", "-r", help="One line on how it went."),
    link: str = typer.Option("", "--link", "-l", help="Where the result is: the posted comment, the PR."),
) -> None:
    """Report how an approved action went; its card shows it."""
    from .actions import ActionStore

    store = ActionStore(_runtime_dir())
    if status == "show":
        found = store.get(action_id)
        if found is None:
            console.print(f"[red]No action {action_id}.[/red]")
            raise typer.Exit(code=1)
        console.print_json(json.dumps(found.to_dict(), ensure_ascii=False))
        return
    try:
        updated = store.report(action_id, status, result, link)
    except (LookupError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    console.print(f"Action {updated.id} is {updated.status}.")


@app.command("actions")
def actions_command(show_all: bool = typer.Option(False, "--all", help="Also the ones finished in the last day and a half.")) -> None:
    """List the actions on Buddy's window."""
    from .actions import OPEN, ActionStore

    shown = [a for a in ActionStore(_runtime_dir()).visible() if show_all or a.status in OPEN]
    if not shown:
        console.print("No open actions.")
    for a in shown:
        stale = " (old)" if a.stale else ""
        console.print(f"{a.id:>4}  {a.status:<9} {a.kind:<11} {a.title}{stale}")


@app.command()
def live(
    show_all: bool = typer.Option(False, "--all", help="Show every turn of the Claude session, not only Buddy's."),
) -> None:
    """Follow Buddy in this terminal: the meeting as it is said, commands, and Claude's work.

    Lines typed here are sent to Buddy like `buddy say`. Ctrl+C leaves.
    """
    import threading

    from .listener import listener_state, submit
    from .live import LiveView, terminal_line

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    runtime = _runtime_dir()
    settings = load_settings()
    view = LiveView(runtime, "all" if show_all else settings.listen.window_shows, keep=settings.listen.window_suggestions_kept)
    console.print("[dim]Following Buddy. Type a command and press Enter to send it; Ctrl+C leaves.[/dim]")

    def read_typed() -> None:
        for line in sys.stdin:
            if line.strip():
                if listener_state(runtime):
                    submit(runtime, line.strip())
                else:
                    console.print("[red]Buddy is off.[/red]")

    if sys.stdin.isatty():
        threading.Thread(target=read_typed, name="buddy-live-input", daemon=True).start()
    shown: list[str] = []
    try:
        while True:
            for item in view.poll():
                console.print(terminal_line(item), highlight=False)
            if view.suggestions != shown:
                shown = list(view.suggestions)
                if shown:
                    console.print("[bold]Suggestions[/bold]\n" + "\n".join(f"  {s}" for s in shown), highlight=False)
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass


def _open_window(runtime: Path) -> None:
    """Start the window in its own process, so it outlives the command that opened it."""
    from .window import window_state

    if window_state(runtime):
        return
    try:
        import webview  # noqa: F401
    except ImportError:
        console.print('[yellow]Buddy\'s window needs pywebview: pip install -e ".[window]"[/yellow]')
        return
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL,
                    "cwd": str(project_root())}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "meeting_agent.cli", "window"], **kwargs)
    console.print("Buddy's window is open.")


@app.command()
def window(
    detach: bool = typer.Option(False, "--detach", help="Open it in the background and return."),
    show_all: bool = typer.Option(False, "--all", help="Show every turn of the Claude session, not only Buddy's."),
) -> None:
    """Open Buddy's floating window: follow and talk to Buddy in one place."""
    from .window import run_window, window_state

    runtime = _runtime_dir()
    if window_state(runtime):
        console.print("Buddy's window is already open.")
        return
    if detach:
        _open_window(runtime)
        return
    try:
        import webview  # noqa: F401
    except ImportError:
        console.print('[red]Buddy\'s window needs pywebview: pip install -e ".[window]"[/red]')
        raise typer.Exit(code=1)
    settings = load_settings()
    runtime.mkdir(parents=True, exist_ok=True)
    run_window(
        runtime,
        "all" if show_all else settings.listen.window_shows,
        settings.listen.window_on_top,
        settings.listen.window_suggestions_kept,
    )


@app.command()
def meeting(action: str = typer.Argument(..., help="start or stop")) -> None:
    """Start or stop recording a meeting in the running listener."""
    from .listener import request

    if action not in {"start", "stop"}:
        console.print("Use `buddy meeting start` or `buddy meeting stop`.")
        raise typer.Exit(code=2)
    _require_listener()
    request(_runtime_dir(), f"{action}-meeting")
    console.print(f"Meeting {action} requested.")


@app.command()
def pause() -> None:
    """Close the microphone until `buddy resume`."""
    from .listener import request

    _require_listener()
    request(_runtime_dir(), "pause")
    console.print("Pause requested.")


@app.command()
def resume() -> None:
    """Reopen the microphone after a pause."""
    from .listener import request

    _require_listener()
    request(_runtime_dir(), "resume")
    console.print("Resume requested.")


# ------------------------------------------------------------------- voices

voices_app = typer.Typer(no_args_is_help=True, help="Voices Buddy remembers for listen.speaker_names (this machine only).")
app.add_typer(voices_app, name="voices")


def _voice_book():
    from .speakers import VoiceBook

    return VoiceBook(_runtime_dir() / "voices.json")


@voices_app.command("setup")
def voices_setup() -> None:
    """Download the local voice model (26 MB, checked against its digest)."""
    from .speakers import download_model

    settings = load_settings()
    if settings.listen.voice_model:
        console.print(f"listen.voice_model is set; using {settings.listen.voice_model}.")
        return
    from .speakers import VOICE_MODEL_FILE, VOICE_MODEL_URL

    target = _runtime_dir() / "models"
    try:
        path = download_model(target)
    except OSError as exc:  # no network, or a TLS-inspecting proxy Python does not trust
        console.print(
            f"[red]Could not download the voice model: {exc}.[/red]\n"
            f"Download {VOICE_MODEL_URL} yourself and save it as {target / VOICE_MODEL_FILE} "
            "then run `buddy voices setup` again to check its digest; or set listen.voice_model."
        )
        raise typer.Exit(code=1)
    console.print(f"Voice model ready: {path}")


@voices_app.command("list")
def voices_list() -> None:
    """Show the remembered voices."""
    book = _voice_book()
    if not book.voices:
        console.print("No voices remembered.")
        return
    table = Table("Name", "Lines heard", "Updated")
    for name in book.names():
        entry = book.voices[name]
        table.add_row(name, str(entry.get("count", "")), str(entry.get("updated", "")))
    console.print(table)


@voices_app.command("forget")
def voices_forget(
    name: str = typer.Argument("", help="The name to forget"),
    everyone: bool = typer.Option(False, "--all", help="Forget every voice"),
) -> None:
    """Delete one remembered voice, or all of them."""
    if not name and not everyone:
        console.print("Name a voice, or pass --all.")
        raise typer.Exit(code=2)
    book = _voice_book()
    removed = book.forget(None if everyone else name)
    book.save()
    console.print(f"Forgot: {', '.join(removed)}" if removed else "Nothing to forget.")


@voices_app.command("name")
def voices_name(
    meeting: str = typer.Argument(..., help="Meeting folder name, e.g. 2026-01-05_100000"),
    voice: str = typer.Argument(..., help="The meeting's voice label, e.g. C"),
    name: str = typer.Argument(..., help="Who it was"),
) -> None:
    """Remember a voice the meeting could not name by itself."""
    from .speakers import name_voice

    settings = load_settings()
    meetings = Path(settings.meetings_dir).expanduser()
    if not meetings.is_absolute():
        meetings = project_root() / meetings
    if not name_voice(meetings / meeting, voice.upper(), name, _voice_book()):
        console.print(f"No voice {voice} in {meetings / meeting / 'voices.json'}.")
        raise typer.Exit(code=1)
    console.print(f"Voice {voice.upper()} of {meeting} is now remembered as {name}.")
@app.command()
def mic(mode: str = typer.Argument("", help="auto, on or off; empty shows the current setting")) -> None:
    """Whether your microphone's meeting lines are kept: follow the meeting app's mute (auto), always, or never."""
    from .mic_gate import MODES, read_mode, write_mode

    path = _runtime_dir() / "listen" / "mic.json"
    if not mode:
        console.print(f"Microphone: {read_mode(path)}")
        return
    if mode not in MODES:
        console.print("Use `buddy mic auto`, `buddy mic on` or `buddy mic off`.")
        raise typer.Exit(code=2)
    write_mode(path, mode)
    console.print(f"Microphone: {mode}")


if __name__ == "__main__":
    app()
