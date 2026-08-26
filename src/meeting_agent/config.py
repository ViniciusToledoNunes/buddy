from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator


class AudioConfig(BaseModel):
    sample_rate: int = 24_000
    chunk_ms: int = Field(100, ge=20, le=1000)
    system_device: str = "default"
    microphone_device: str = "default"
    queue_seconds: int = Field(12, ge=2, le=120)


class ASRConfig(BaseModel):
    cloud_model: str = "gpt-live-transcribe"
    local_model_en: str = "tiny.en"
    local_model_multilingual: str = "base"
    compute_type: str = "int8"
    beam_size: int = Field(1, ge=1, le=10)
    vad_threshold: float = Field(0.012, ge=0.0001, le=1)
    min_speech_ms: int = Field(250, ge=50)
    silence_ms: int = Field(600, ge=100)
    max_segment_seconds: float = Field(12, ge=2, le=60)


class CopilotConfig(BaseModel):
    enabled: bool = True
    automatic_suggestions: bool = True
    suggestion_cooldown_seconds: int = Field(20, ge=5)
    context_minutes: int = Field(5, ge=1, le=30)
    output_language: str = "en"
    openai_model: str = "gpt-5-mini"
    anthropic_model: str = "claude-sonnet-4-5"
    ollama_model: str = "qwen3:4b"


class HotkeyConfig(BaseModel):
    toggle_meeting: str = "ctrl+alt+m"
    suggest_now: str = "ctrl+alt+space"


class UIConfig(BaseModel):
    refresh_hz: int = Field(8, ge=1, le=30)
    overlay: bool = False


class BenchmarkConfig(BaseModel):
    source: str = r"C:\Users\vinic\Videos\trr01.mp4"
    seconds: int = Field(60, ge=5, le=600)
    models: list[str] = ["tiny.en", "base.en", "distil-small.en"]


class Settings(BaseModel):
    language: Literal["en", "pt", "auto"] = "en"
    asr_mode: Literal["auto", "cloud-fast", "local-gpu", "local-cpu"] = "auto"
    llm_provider: Literal["auto", "openai", "anthropic", "ollama", "disabled"] = "auto"
    save_audio: bool = False
    meetings_dir: str = "meetings"
    audio: AudioConfig = AudioConfig()
    asr: ASRConfig = ASRConfig()
    copilot: CopilotConfig = CopilotConfig()
    hotkeys: HotkeyConfig = HotkeyConfig()
    ui: UIConfig = UIConfig()
    benchmark: BenchmarkConfig = BenchmarkConfig()

    @model_validator(mode="after")
    def cloud_requires_opt_in(self) -> "Settings":
        # Merely having a key does not transmit audio: auto selects cloud only when
        # BUDDY_ALLOW_CLOUD_AUDIO is explicitly true (legacy variable also works).
        return self


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    override = os.getenv("BUDDY_CONFIG") or os.getenv("MEETING_AGENT_CONFIG")
    return Path(override).expanduser().resolve() if override else project_root() / "config.yaml"


def load_settings(path: Path | None = None) -> Settings:
    load_dotenv(project_root() / ".env", override=False)
    target = path or config_path()
    if not target.exists():
        example = project_root() / "config.example.yaml"
        target = example
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data)


def cloud_audio_allowed() -> bool:
    return any(
        os.getenv(name, "").lower() in {"1", "true", "yes"}
        for name in ("BUDDY_ALLOW_CLOUD_AUDIO", "MEETING_AGENT_ALLOW_CLOUD_AUDIO")
    )
