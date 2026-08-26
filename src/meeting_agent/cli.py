from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import typer
from pynput import keyboard
from rich.console import Console
from rich.table import Table

from .asr import select_asr
from .audio import list_audio_devices
from .benchmark import run_benchmark
from .config import config_path, load_settings, project_root
from .doctor import run_doctor
from .session import STATE_FILE, _hotkey, _state_process_alive, read_state, request_stop, run_session


app = typer.Typer(no_args_is_help=True, help="Low-latency cross-platform Meeting Copilot")
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
    console.print("[bold red]RECORDING / TRANSCRIBING[/bold red]")
    console.print(f"ASR: {selection.mode} / {selection.model} / {selection.compute_type} ({selection.reason})")
    console.print(f"Stop: meeting-agent stop or {settings.hotkeys.toggle_meeting}; suggest: {settings.hotkeys.suggest_now}")
    try:
        directory = asyncio.run(run_session(settings))
        console.print(f"Meeting saved to [bold]{directory}[/bold]")
    except KeyboardInterrupt:
        request_stop()
        console.print("Stopping…")
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
    """Show current session status."""
    state = read_state()
    if not state or not _state_process_alive(state):
        console.print("[dim]STOPPED — no capture is active.[/dim]")
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
    listener = keyboard.GlobalHotKeys({_hotkey(settings.hotkeys.toggle_meeting): toggle})
    listener.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        listener.stop()


if __name__ == "__main__":
    app()
