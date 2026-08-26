from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass

import ctranslate2
import psutil
import httpx

from .audio import capture_capabilities, probe_audio
from .config import Settings, cloud_audio_allowed
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
        checks.append(Check("NVIDIA GPU/CUDA", "not-needed", "not present; local-cpu/cloud-fast supported"))
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
    if os.getenv("OPENAI_API_KEY"):
        try:
            response = httpx.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                timeout=8,
            )
            response.raise_for_status()
            detail = "authenticated; cloud audio explicitly allowed" if cloud_audio_allowed() else "authenticated; cloud audio opt-in disabled"
            checks.append(Check("OpenAI API", "ok", detail))
        except Exception as exc:
            checks.append(Check("OpenAI API", "failed", f"authentication/connectivity: {type(exc).__name__}"))
    else:
        checks.append(Check("OpenAI API", "not-needed", "OPENAI_API_KEY absent; local ASR available"))
    if os.getenv("OPENAI_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or settings.llm_provider == "ollama":
        checks.append(Check("LLM", "ok", f"configured provider: {settings.llm_provider}"))
    else:
        checks.append(Check("LLM", "warning", "no provider key; transcription works, suggestions/report use fallback"))
    return checks


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
