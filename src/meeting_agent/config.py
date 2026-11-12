from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator


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
    # Options of the Claude Code harness (brain.harness: claude-code).
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

    # A command stays open after "Hey Buddy" until one of these closing phrases, so a
    # pause mid-sentence no longer sends half an instruction.
    end_phrases: list[str] = ["over and out", "that's all buddy", "that is all buddy"]
    # Fallbacks for a forgotten closing phrase; the session is told the command may be cut.
    command_silence_seconds: float = Field(10.0, ge=2, le=120)
    command_max_seconds: float = Field(120.0, ge=10, le=900)
    # A short tone when a command opens, is sent, or is cancelled (Windows).
    sounds: bool = True
    # Buddy's floating window: the meeting as it is said, commands, and what the Claude
    # Code session watching Buddy does with them. It opens with `buddy listen --detach`.
    window: bool = True
    window_on_top: bool = True
    # The session's turns to show: only those Buddy started, or all of them.
    window_shows: Literal["buddy", "all"] = "buddy"
    # Suggestion sets kept on screen, newest at the bottom: a new set must not wipe the one
    # the user is still reading.
    window_suggestions_kept: int = Field(3, ge=1, le=10)
    # A meeting batch goes out at a pause, at most once per interval, and never waits
    # longer than max_wait. A session turn takes 40-90s, so sending faster only queues.
    batch_min_seconds: float = Field(60.0, ge=5, le=900)
    batch_pause_seconds: float = Field(3.0, ge=0.5, le=60)
    batch_max_wait_seconds: float = Field(150.0, ge=10, le=1800)
    # Without headphones the microphone also hears the other side. A meeting line from
    # the microphone is held this long and dropped if the same words arrive as system
    # audio around it. 0 keeps every line and writes it at once.
    echo_window_seconds: float = Field(8.0, ge=0, le=30)
    # On: between meetings the microphone waits for "Hey Buddy", and "Hey Buddy ..." is a
    # command in a meeting too. Off (the default): the microphone opens only while a
    # meeting is recorded (the Meeting button, the hotkey or `buddy meeting start`) or
    # while the window's 🎤 is on, and closes again after; commands are typed or dictated.
    wake_phrase: bool = False
    # The window's 🎤 button: speech while it is on becomes text in the window's box, to
    # read and send. How long after the second click the last words are still awaited.
    dictation_grace_seconds: float = Field(2.5, ge=0, le=10)
    # The work board's ticket keys link here; {key} is replaced, e.g.
    # "https://example.atlassian.net/browse/{key}". Empty: keys are shown without a link.
    board_ticket_url: str = ""
    # Screenshots of the main display during a meeting (macOS), saved in the meeting's
    # `screens/` folder and named in the next batch, so the watching session can see
    # what is being shared. Off by default: it captures the whole screen. A capture
    # identical to the previous one is dropped.
    screen_capture: bool = False
    screen_interval_seconds: float = Field(60.0, ge=10, le=900)
    screen_max_width: int = Field(1600, ge=0, le=6000)
    screen_max_per_meeting: int = Field(240, ge=1, le=5000)
    # Who on the other side is talking. "screen": the name on the tile the meeting app
    # outlines while someone talks (Teams, macOS). "voice": a local voice model groups
    # lines by voice ("voice B"). "both": the screen names the voices, and a named voice
    # keeps its name when the screen shows nobody. Off by default: it captures the screen
    # while the other side talks, and "voice" computes voice embeddings.
    speaker_names: Literal["off", "screen", "voice", "both"] = "off"
    speaker_screen_seconds: float = Field(2.0, ge=0.5, le=30)  # at most one look at the screen this often
    voice_threshold: float = Field(0.6, ge=0.3, le=0.95)  # cosine similarity for "the same voice"
    speaker_votes_needed: int = Field(2, ge=1, le=20)  # screen sightings before a voice's name is settled
    # Keep the voices the screen named (one mean embedding per name, never audio) in the
    # runtime folder, so they are recognised in later meetings without the screen.
    # `buddy voices list|forget` shows and deletes them.
    remember_voices: bool = False
    voice_model: str = ""  # an ONNX WeSpeaker model; empty: the one `buddy voices setup` downloads
    speaker_self_names: list[str] = []  # your own tile's name, never given to a REMOTE line
    # Keep your microphone's meeting lines only while the meeting app can hear you (macOS:
    # Teams, Google Meet in Chrome/Edge/Arc/Brave, Zoom), read from its microphone button
    # through the Accessibility permission. Commands to Buddy work either way.
    # `buddy mic on|off|auto` overrides it. With no app read: keep or drop the lines.
    mic_follows_mute: bool = False
    mic_when_unknown: Literal["keep", "drop"] = "keep"
    # A question from the other side sends the current batch at once instead of waiting
    # for the interval. A line counts when it has a "?" and more than three words ("Right?"
    # is a tag, not a question), or names the user.
    # When no agent session follows the listener, Buddy runs its own brain (the `brain`
    # harness, headless) on each batch, shows its suggestions in the window and writes the
    # meeting summary. A watching session always takes over.
    auto_brain: bool = True
    question_alerts: bool = True
    question_names: list[str] = []          # e.g. ["Vinicius", "Vinny"]: always count, even when short
    question_min_gap_seconds: float = Field(10.0, ge=0, le=600)  # unnamed questions closer than this ride in the next batch
    # A meeting nobody ended still ends: silence, or a ceiling on its length.
    meeting_idle_minutes: float = Field(10.0, ge=1, le=240)
    meeting_max_minutes: float = Field(240.0, ge=5, le=1440)
    event_log_max_mb: float = Field(5.0, ge=0.1, le=500)
    event_line_max_chars: int = Field(6000, ge=500, le=50_000)

    @field_validator("speaker_names", mode="before")
    @classmethod
    def _yaml_off(cls, value: object) -> object:
        return "off" if value is False else value  # YAML reads a bare `off` as false


class HotkeyConfig(BaseModel):
    toggle_meeting: str = "ctrl+alt+m"
    suggest_now: str = "ctrl+alt+space"


class UIConfig(BaseModel):
    refresh_hz: int = Field(8, ge=1, le=30)
    overlay: bool = False


class BenchmarkConfig(BaseModel):
    # Any recording of your own: `buddy benchmark` measures the ASR models against it.
    source: str = "samples/meeting.mp4"
    seconds: int = Field(60, ge=5, le=600)
    models: list[str] = ["tiny.en", "base.en", "distil-small.en"]


class CursorBrainConfig(BaseModel):
    """Options of the Cursor harness (brain.harness: cursor)."""

    # Without it the CLI refuses every shell command, and the agent spent 56s of one
    # turn retrying a refused command instead of answering.
    force: bool = True
    timeout_seconds: float = Field(240.0, ge=20, le=900)


class BrainConfig(BaseModel):
    """The agent Buddy runs headless as its brain (llm_provider: harness, and the auto brain)."""

    harness: Literal["claude-code", "cursor"] = "claude-code"
    model: str = ""  # empty: the harness's own default
    # Claude Code only; Cursor names the effort in the model, as in gpt-5.3-codex-high.
    effort: Literal["", "low", "medium", "high", "xhigh", "max"] = ""
    # The brain runs here, so it loads the context files that describe the user's work.
    workdir: str = ""
    cursor: CursorBrainConfig = CursorBrainConfig()

    def directory(self) -> Path:
        configured = self.workdir.strip()
        return Path(configured).expanduser() if configured else Path.home()


class MemoryConfig(BaseModel):
    """The user's working memory, read and kept by Buddy's brain and the user's coding agents."""

    # memory/ is gitignored: it holds the user's work. memory.example/ is the empty skeleton
    # it starts from. A relative path is taken from the Buddy folder.
    dir: str = "memory"
    # While the listener runs, changes from any agent go into the folder's own git, so a bad
    # write is one `git revert` away. 0 turns it off.
    autocommit_seconds: float = Field(60.0, ge=0, le=3600)
    # Folders the brain may read for context and never changes: past agent conversations,
    # agent rules, repositories. Leave out anything that holds credentials.
    sources: list[str] = []
    # Local times (HH:MM) when the brain checks the memory against the real state of the work.
    refresh_at: list[str] = []

    @field_validator("refresh_at")
    @classmethod
    def _times(cls, value: list[str]) -> list[str]:
        for moment in value:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", moment):
                raise ValueError(f"refresh_at takes HH:MM times, not {moment!r}")
        return sorted(set(value))

    def source_paths(self) -> list[Path]:
        """The sources that exist here, files or folders."""
        return [path for path in (Path(s).expanduser() for s in self.sources) if path.exists()]

    def directory(self) -> Path:
        path = Path(self.dir).expanduser()
        return path if path.is_absolute() else project_root() / path


class SlackConfig(BaseModel):
    """New Slack messages, from the log a separate Slack watcher writes (see slack_watch.py)."""

    enabled: bool = False
    # The watcher's log, one file per day: {date} becomes YYYY-MM-DD.
    log: str = ""
    # Bots and busy channels that never need the user; everything else goes to the brain,
    # which decides with the user's memory what deserves a card.
    ignore_channels: list[str] = []
    ignore_authors: list[str] = []
    # How the brain reads a conversation, builds a link to a message, and what it must never run.
    instructions: str = ""
    poll_seconds: float = Field(15.0, ge=2, le=600)


# Configuration written before the harness could be chosen named Claude Code's settings.
LEGACY_BRAIN_KEYS = {"claude_workdir": "workdir", "claude_model": "model", "claude_effort": "effort"}


class Settings(BaseModel):
    language: Literal["en", "pt", "auto"] = "en"
    asr_mode: Literal["auto", "local-gpu", "local-cpu"] = "auto"
    llm_provider: Literal["auto", "harness", "openai", "anthropic", "ollama", "disabled"] = "auto"
    save_audio: bool = False
    meetings_dir: str = "meetings"
    # Extra env files to load, the way a wrapper script sources them. Credentials that
    # live in a file and are never exported are invisible to a plain environment read.
    env_files: list[str] = []
    brain: BrainConfig = BrainConfig()
    memory: MemoryConfig = MemoryConfig()
    slack: SlackConfig = SlackConfig()
    audio: AudioConfig = AudioConfig()
    asr: ASRConfig = ASRConfig()
    copilot: CopilotConfig = CopilotConfig()
    listen: ListenConfig = ListenConfig()
    hotkeys: HotkeyConfig = HotkeyConfig()
    ui: UIConfig = UIConfig()
    benchmark: BenchmarkConfig = BenchmarkConfig()

    @model_validator(mode="before")
    @classmethod
    def _legacy_brain(cls, data: object) -> object:
        """`llm_provider: claude-code` and `copilot.claude_workdir/model/effort` still work.

        They become the Claude Code harness and its `brain` settings; a key the `brain`
        block sets itself wins over its legacy twin.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        raw_brain = data.get("brain")
        brain = dict(raw_brain) if isinstance(raw_brain, dict) else {}
        copilot = data.get("copilot")
        if isinstance(copilot, dict):
            copilot = dict(copilot)
            for legacy, key in LEGACY_BRAIN_KEYS.items():
                if legacy in copilot:
                    brain.setdefault(key, copilot.pop(legacy))
            data["copilot"] = copilot
        if data.get("llm_provider") == "claude-code":
            data["llm_provider"] = "harness"
            brain.setdefault("harness", "claude-code")
        if brain and (raw_brain is None or isinstance(raw_brain, dict)):
            data["brain"] = brain
        return data


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


_BRAIN_BLOCK = re.compile(r"^brain:[ \t]*(#.*)?$")
_BLOCK_KEY = re.compile(r"^(\s+)(\w+):\s*([^#]*?)(\s*#.*)?$")


def _yaml_quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _with_brain_choice(text: str, values: dict[str, str]) -> str:
    lines = text.splitlines()
    if any(line.startswith("brain:") and not _BRAIN_BLOCK.match(line) for line in lines):
        raise ValueError("the brain block is written inline; edit it by hand")
    start = next((i for i, line in enumerate(lines) if _BRAIN_BLOCK.match(line)), None)
    if start is None:
        block = ["brain:", *(f"  {key}: {_yaml_quoted(value)}" for key, value in values.items())]
        return text.rstrip("\n") + "\n\n" + "\n".join(block) + "\n"
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
        end += 1
    children = [line for line in lines[start + 1:end] if line.strip() and not line.lstrip().startswith("#")]
    indent = min((len(line) - len(line.lstrip()) for line in children), default=2)
    pending = dict(values)
    for index in range(start + 1, end):
        match = _BLOCK_KEY.match(lines[index])
        if match and len(match.group(1)) == indent and match.group(2) in pending:
            key = match.group(2)
            lines[index] = f"{match.group(1)}{key}: {_yaml_quoted(pending.pop(key))}{match.group(4) or ''}"
    lines[start + 1:start + 1] = [f"{' ' * indent}{key}: {_yaml_quoted(value)}" for key, value in pending.items()]
    return "\n".join(lines) + "\n"


def choose_brain(harness: str, model: str = "", path: Path | None = None) -> Path | None:
    """Writes brain.harness and brain.model into the configuration, keeping its comments.

    The model is written even when empty: model names differ between harnesses, so the
    previous harness's model would be refused by the next one. Returns the backup made of
    the file it changed, if there was one.
    """
    target = path or config_path()
    source = target if target.exists() else project_root() / "config.example.yaml"
    updated = _with_brain_choice(source.read_text(encoding="utf-8"), {"harness": harness, "model": model})
    written = Settings.model_validate(yaml.safe_load(updated) or {}).brain
    if (written.harness, written.model) != (harness, model):
        raise ValueError(f"could not write the brain choice into {target}; edit its brain block by hand")
    backup = None
    if target.exists():
        backup = target.with_name(f"{target.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(target, backup)
    target.write_text(updated, encoding="utf-8")
    return backup


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

