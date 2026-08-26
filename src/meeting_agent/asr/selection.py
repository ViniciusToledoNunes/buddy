from __future__ import annotations

import os
import shutil
import json
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings, cloud_audio_allowed


@dataclass(frozen=True, slots=True)
class ASRSelection:
    mode: str
    model: str
    device: str
    compute_type: str
    reason: str


def has_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None


def select_asr(settings: Settings) -> ASRSelection:
    requested = settings.asr_mode
    language = settings.language
    local_model = settings.asr.local_model_en if language == "en" else settings.asr.local_model_multilingual
    benchmark_path = Path(__file__).resolve().parents[3] / "benchmark-results.json"
    if requested == "auto" and language == "en" and benchmark_path.exists():
        try:
            recommended = json.loads(benchmark_path.read_text(encoding="utf-8")).get("recommended")
            if recommended:
                local_model = str(recommended)
        except (OSError, ValueError, TypeError):
            pass
    if requested == "cloud-fast":
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("cloud-fast requires OPENAI_API_KEY")
        if not cloud_audio_allowed():
            raise RuntimeError(
                "cloud-fast requires explicit consent: set MEETING_AGENT_ALLOW_CLOUD_AUDIO=true in .env"
            )
        return ASRSelection("cloud-fast", settings.asr.cloud_model, "OpenAI", "PCM16", "explicit config")
    if requested == "local-gpu":
        if not has_nvidia():
            raise RuntimeError("local-gpu requested but no NVIDIA GPU/driver was found")
        return ASRSelection("local-gpu", local_model, "cuda", "float16", "explicit config")
    if requested == "local-cpu":
        return ASRSelection("local-cpu", local_model, "cpu", settings.asr.compute_type, "explicit config")

    if os.getenv("OPENAI_API_KEY") and cloud_audio_allowed():
        return ASRSelection(
            "cloud-fast", settings.asr.cloud_model, "OpenAI", "PCM16", "API key and cloud audio opt-in"
        )
    if has_nvidia():
        return ASRSelection("local-gpu", local_model, "cuda", "float16", "NVIDIA GPU detected")
    reason = "local benchmark recommendation" if benchmark_path.exists() else "CPU-safe automatic fallback"
    return ASRSelection("local-cpu", local_model, "cpu", settings.asr.compute_type, reason)
