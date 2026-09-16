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
    # A capture stream that carries only digital silence for this long is treated as
    # lost and the device is re-resolved. 0 disables the check.
    silence_reconnect_seconds: float = Field(20.0, ge=0, le=600)


class ASRConfig(BaseModel):
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
    suggestion_refresh_seconds: float = Field(6.0, ge=0.05, le=300)
    suggestion_debounce_seconds: float = Field(1.5, ge=0.01, le=30)
    context_minutes: int = Field(5, ge=1, le=30)
    # A reasoning model with a full meeting prompt regularly needs more than 25s.
    request_timeout_seconds: float = Field(45.0, ge=5, le=300)
    semantic_memory_enabled: bool = True
    semantic_memory_max_meetings: int = Field(50, ge=1, le=500)
    semantic_memory_matches: int = Field(3, ge=1, le=10)
    output_language: str = "en"
    # Tier 1 runs every few seconds, so it wants the fastest model; tier 2 runs on
    # demand with project tools and wants the strongest.
    openai_model: str = "gpt-5.4-mini"
    # gpt-5 models reason by default, which costs tens of seconds on a full meeting
    # prompt. The panel refreshes every few seconds, so it asks for none.
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] = "none"
    anthropic_model: str = "claude-haiku-4-5"
    project_context_enabled: bool = True
    project_context_matches: int = Field(3, ge=1, le=10)
    prior_meetings_in_context: int = Field(20, ge=1, le=200)
    # The investigator runs a tool loop in the background when the reflex loop raises a
    # question it could not answer. Turns are bounded so a model that keeps calling
    # tools cannot run for the whole meeting.
    investigation_enabled: bool = True
    investigation_model: str = "gpt-5.4"
    investigation_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] = "low"
    investigation_max_turns: int = Field(8, ge=2, le=30)
    investigation_timeout_seconds: float = Field(180.0, ge=10, le=900)
    investigations_in_flight: int = Field(1, ge=1, le=4)
    # Connectors reach real company data, so they are read-only and a query that would
    # scan more than the limit is refused on the free dry run instead of being billed.
    # The brain: the user's own Claude Code, run headless (llm_provider: claude-code).
    # It runs in claude_workdir so it loads the CLAUDE.md that describes the user's work.
    claude_workdir: str = ""
    claude_model: str = ""
    claude_effort: Literal["", "low", "medium", "high", "xhigh", "max"] = ""
    claude_timeout_seconds: float = Field(240.0, ge=20, le=900)
    # User-level settings are left out on purpose: they commonly allow git push and
    # similar, which an unattended run must never inherit.
    claude_setting_sources: str = "project"
    claude_allowed_tools: list[str] = [
        "Read",
        "Grep",
        "Glob",
        "WebSearch",
        "WebFetch",
        "Bash(git log *)",
        "Bash(git show *)",
        "Bash(git diff *)",
        "Bash(git blame *)",
    ]
    claude_disallowed_tools: list[str] = [
        "Edit",
        "Write",
        "NotebookEdit",
        "PowerShell",
        "Bash(git commit *)",
        "Bash(git push *)",
        "Bash(git reset *)",
        "Bash(git checkout *)",
        "Bash(rm *)",
    ]
    claude_extra_dirs: list[str] = []
    claude_connectors: bool = True
    claude_mcp: bool = False
    claude_instructions: str = ""
    bigquery_enabled: bool = True
    # The project that pays for the query, which is not where the data lives.
    bigquery_billing_project: str = ""
    # Projects whose datasets are worth exploring. Tables are addressed fully qualified,
    # so the billing project having no data of its own is fine.
    bigquery_data_projects: list[str] = []
    bigquery_max_scan_gb: float = Field(20.0, ge=0.1, le=1000)
    jira_enabled: bool = True
    datadog_enabled: bool = True
    ollama_model: str = "qwen3:4b"


class ListenConfig(BaseModel):
    """`buddy listen`: an always-attentive microphone that keeps only what matters."""

    # After a bare "Hey Buddy", how long the next utterance counts as the command.
    wake_arm_seconds: float = Field(6.0, ge=1, le=30)
    # A meeting batch goes out at a pause, at most once per interval, and never waits
    # longer than max_wait. A session turn takes 40-90s, so sending faster only queues.
    batch_min_seconds: float = Field(60.0, ge=5, le=900)
    batch_pause_seconds: float = Field(3.0, ge=0.5, le=60)
    batch_max_wait_seconds: float = Field(150.0, ge=10, le=1800)
    # A meeting nobody ended still ends: silence, or a ceiling on its length.
    meeting_idle_minutes: float = Field(10.0, ge=1, le=240)
    meeting_max_minutes: float = Field(240.0, ge=5, le=1440)
    event_log_max_mb: float = Field(5.0, ge=0.1, le=500)
    event_line_max_chars: int = Field(6000, ge=500, le=50_000)


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
    asr_mode: Literal["auto", "local-gpu", "local-cpu"] = "auto"
    llm_provider: Literal["auto", "claude-code", "openai", "anthropic", "ollama", "disabled"] = "auto"
    save_audio: bool = False
    meetings_dir: str = "meetings"
    # Extra env files to load, the way a wrapper script sources them. Credentials that
    # live in a file and are never exported are invisible to a plain environment read.
    env_files: list[str] = []
    audio: AudioConfig = AudioConfig()
    asr: ASRConfig = ASRConfig()
    copilot: CopilotConfig = CopilotConfig()
    listen: ListenConfig = ListenConfig()
    hotkeys: HotkeyConfig = HotkeyConfig()
    ui: UIConfig = UIConfig()
    benchmark: BenchmarkConfig = BenchmarkConfig()


def anthropic_workspace_id() -> str:
    """Workspace an identity-linked API key acts in.

    Keys tied to a user identity are rejected with HTTP 400 unless the request names a
    workspace. Plain workspace keys ignore the header, so sending it when set is safe
    for both kinds.
    """
    return os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()


def anthropic_headers() -> dict[str, str]:
    workspace = anthropic_workspace_id()
    return {"anthropic-workspace-id": workspace} if workspace else {}


def anthropic_client_options(timeout: float) -> dict[str, object]:
    """Constructor options for anthropic.AsyncAnthropic, workspace header included."""
    options: dict[str, object] = {"timeout": timeout}
    headers = anthropic_headers()
    if headers:
        options["default_headers"] = headers
    return options


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
    settings = Settings.model_validate(data)
    load_env_files(settings)
    return settings


def load_env_files(settings: Settings) -> list[str]:
    """Source the extra env files named in configuration.

    Credentials often live in a file that a wrapper script sources per command and never
    exports, so reading the environment alone reports them as absent.
    """
    loaded: list[str] = []
    for entry in settings.env_files:
        candidate = Path(entry).expanduser()
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            loaded.append(str(candidate))
    return loaded

