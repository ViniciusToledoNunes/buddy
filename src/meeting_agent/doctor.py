from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import ctranslate2
import psutil
import httpx

from .audio import capture_capabilities, probe_audio
from .config import Settings, anthropic_headers
from .system import find_ffmpeg
from .config import project_root


@dataclass(slots=True)
class Check:
    name: str
    state: str
    detail: str


def run_doctor(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("Python", "ok", platform.python_version()))
    ffmpeg = find_ffmpeg()
    checks.append(Check("FFmpeg", "ok" if ffmpeg else "failed", ffmpeg or "not found"))
    checks.append(
        Check(
            "CPU/RAM",
            "ok",
            f"{platform.processor() or 'x64'}, {psutil.cpu_count(logical=False)}C/{psutil.cpu_count()}T, "
            f"{psutil.virtual_memory().total / 1024**3:.2f} GB",
        )
    )
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        result = subprocess.run([nvidia, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"], capture_output=True, text=True)
        checks.append(Check("NVIDIA GPU/CUDA", "ok", result.stdout.strip() or result.stderr.strip()))
    else:
        checks.append(Check("NVIDIA GPU/CUDA", "not-needed", "not present; local-cpu transcription supported"))
    try:
        compute = ", ".join(sorted(ctranslate2.get_supported_compute_types("cpu")))
        checks.append(Check("CTranslate2 ASR", "ok", f"{ctranslate2.__version__}; CPU: {compute}"))
    except Exception as exc:
        checks.append(Check("CTranslate2 ASR", "failed", str(exc)))
    cpp = project_root() / "tools" / "whispercpp" / "Release" / "whisper-cli.exe"
    cpp_model = project_root() / "models" / "whispercpp" / "ggml-base.en-q5_1.bin"
    checks.append(
        Check(
            "whisper.cpp",
            "ok" if cpp.exists() and cpp_model.exists() else "not-needed",
            "portable b4938; base.en q5_1" if cpp.exists() and cpp_model.exists() else "optional benchmark control not installed",
        )
    )
    capabilities = capture_capabilities()
    checks.append(Check("Audio backend", "ok", f"{capabilities['platform']} / {capabilities['backend']}"))
    for result in probe_audio(settings.audio):
        checks.append(Check(result["name"], result["state"], result["detail"]))
    if os.getenv("ANTHROPIC_API_KEY") and settings.llm_provider in {"anthropic", "auto"}:
        # A real one-token completion, not /v1/models. Listing models succeeds on an
        # account with no credit balance, so the cheap check certifies a pipeline that
        # cannot answer a single request.
        try:
            response = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(),
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                    **anthropic_headers(),
                },
                json={
                    "model": settings.copilot.anthropic_model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=20,
            )
            if response.status_code == 200:
                checks.append(
                    Check("Anthropic API", "ok", f"{settings.copilot.anthropic_model} answered; audio stays local")
                )
            else:
                try:
                    reason = response.json().get("error", {}).get("message", "")
                except ValueError:
                    reason = response.text[:200]
                checks.append(Check("Anthropic API", "failed", f"HTTP {response.status_code}: {reason[:220]}"))
        except Exception as exc:
            checks.append(Check("Anthropic API", "failed", f"{type(exc).__name__}: {exc}"))
    else:
        reason = (
            f"not used by llm_provider {settings.llm_provider}"
            if os.getenv("ANTHROPIC_API_KEY")
            else "ANTHROPIC_API_KEY absent; local ASR still works"
        )
        checks.append(Check("Anthropic API", "not-needed", reason))
    checks.append(brain_check(settings))
    return checks


def brain_check(settings: Settings) -> Check:
    """Whether the configured suggestion engine can actually run.

    A real Claude Code run loads the whole work context, which is too heavy for a health
    check, so this confirms the pieces a run needs instead of spending one.
    """
    provider = settings.llm_provider
    if provider == "disabled":
        return Check("LLM", "not-needed", "suggestions disabled; transcription still works")
    if provider == "claude-code":
        from .claude_code import claude_binary

        binary = claude_binary()
        workdir = Path(settings.copilot.claude_workdir).expanduser() if settings.copilot.claude_workdir else Path.home()
        if binary is None:
            return Check("LLM", "failed", "claude-code selected but the claude CLI is not installed")
        if not workdir.is_dir():
            return Check("LLM", "failed", f"claude_workdir does not exist: {workdir}")
        context = [name for name in ("CLAUDE.md", "CLAUDE.local.md") if (workdir / name).is_file()]
        detail = f"Claude Code in {workdir}; context files: {', '.join(context) or 'none found'}"
        return Check("LLM", "ok" if context else "warning", detail)
    if provider == "ollama" or os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY"):
        return Check("LLM", "ok", f"configured provider: {provider}")
    return Check("LLM", "warning", "no provider available; transcription works, suggestions/report do not")


def hardware_snapshot() -> dict[str, object]:
    return {
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "cpu": platform.processor(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / 1024**3, 2),
        "nvidia": bool(shutil.which("nvidia-smi")),
        "ffmpeg": find_ffmpeg(),
    }
