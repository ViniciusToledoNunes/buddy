# Buddy — Meeting Copilot

[![CI](https://github.com/ViniciusToledoNunes/buddy/actions/workflows/ci.yml/badge.svg)](https://github.com/ViniciusToledoNunes/buddy/actions/workflows/ci.yml)

Buddy keeps your microphone attentive for "Hey Buddy", records meetings when you ask, and turns the coding
agent you already have open into a meeting copilot: it suggests what you could say next, grounded in your own
repositories, tickets and dashboards rather than in generic advice.

Audio never leaves the machine. Transcription is always local, with `faster-whisper`/CTranslate2 on CPU or GPU;
there is no cloud ASR mode. What reaches a model is text: the recent transcript, the meeting memory, and
whatever the agent decides to read with the read-only tools you allow it.

> **Project status.** A personal project, developed in the open. Windows 11 is the validated platform. Linux and
> macOS capture backends are implemented and exercised in CI, but not yet tested end to end with real audio. The
> listening mode needs [Claude Code](https://claude.com/claude-code) today; making it work with Codex, Cursor and
> other agents is the next milestone (see [the roadmap](docs/ROADMAP.md)).

## Two ways to run it

**Listening mode (`buddy listen`) — recommended.** A small background process keeps the microphone open for the
wake phrase. Speech that is neither a command nor part of a meeting is transcribed in memory and dropped. What
it notices goes to an append-only event log, and an agent session you already have open reads that log and acts:
it answers your voice commands and, during a meeting, suggests what to say. No window of its own.

**Session mode (`buddy start`).** One meeting at a time, with a terminal UI of its own and an optional
always-on-top overlay. Buddy calls the model itself and keeps a live suggestion panel.

## Requirements

- Python 3.12, 3.13 or 3.14.
- A microphone and an output device the system recognises.
- About 2 GB free for the virtual environment, caches and a local ASR model.
- For the listening mode: Claude Code, signed in.
- FFmpeg, for the benchmark. Live capture does not need it on every backend.

| Platform | System audio | Microphone | State |
|---|---|---|---|
| Windows 10/11 | WASAPI loopback | WASAPI | Validated on real hardware |
| Linux desktop | PipeWire sink capture | PipeWire source | Implemented; needs validation on target hardware |
| macOS 15+ | ScreenCaptureKit | ScreenCaptureKit | Implemented; needs validation on target hardware |

See [docs/PORTABILITY.md](docs/PORTABILITY.md) for per-platform prerequisites.

## Install

```sh
git clone https://github.com/ViniciusToledoNunes/buddy.git
cd buddy
```

Windows (PowerShell):

```powershell
.\scripts\install-windows.ps1
.\.venv\Scripts\Activate.ps1
buddy doctor
```

Linux:

```sh
./scripts/install-linux.sh
. .venv/bin/activate
buddy doctor
```

macOS 15+:

```sh
./scripts/install-macos.sh
. .venv/bin/activate
buddy doctor
```

The installers also copy the skills into `~/.claude/skills/` and `~/.codex/skills/` when those clients exist, and
register the local MCP server under the name `buddy`. Copy `config.example.yaml` to `config.yaml` and
`.env.example` to `.env` to configure it; both are gitignored.

## Listening mode

```sh
buddy listen --detach      # microphone attentive for "Hey Buddy", in the background
buddy status               # listening, meeting or paused
buddy listen --stop        # turn it off
```

Then, in the Claude Code session you keep open, say "turn Buddy on". The `buddy-listener` skill arms a monitor
over `buddy watch --follow --as claude-code` and starts handling events.

**Talking to it.** A command opens with "Hey Buddy" and closes with **"over and out"** (or "that's all, Buddy").
Pause mid-sentence as much as you like: nothing is dispatched before the closing phrase, so an instruction is
never acted on half-finished. "Cancel" or "never mind" drops it. If you forget to close, Buddy sends what it has
after 10 seconds of silence and tells the session the command may be incomplete. Short tones mark a command
opening, being sent and being cancelled (`listen.sounds: false` turns them off).

| You say | Handled by | What happens |
|---|---|---|
| "Hey Buddy, the meeting is starting" | Buddy, in ~2s | records microphone and system audio |
| "Hey Buddy, the meeting is over" | Buddy | ends it; the agent writes the summary and the follow-ups |
| "Hey Buddy, stop listening" | Buddy | closes the microphone until `buddy resume` |
| "Hey Buddy, stop" / "shut down" / "turn off" | Buddy | ends the process |
| "Hey Buddy, review PR 123. Over and out." | the agent | investigates and answers in the session |
| "Hey Buddy, post the update on Slack. Over and out." | the agent | drafts it and waits for you to approve the text |
| "Hey Buddy, approve 4. Over and out." | the agent | carries out proposal 4 exactly as proposed |

Control commands — meetings, pause, shutdown — take effect immediately, need no closing phrase, and never reach
the agent. Commands are in English: the ASR uses an English-only model, which is faster and more accurate.

**What is kept.** Speech that does not open with the wake phrase and is not part of a meeting is transcribed in
memory and discarded. Meetings are recorded in full, about 44 KB of text per hour. System audio is captured only
while a meeting is being recorded.

**During a meeting**, the session receives a batch of speech at pauses, at most once a minute: one notification
per utterance would flood the monitor, and an agent turn takes 40 to 90 seconds.

**A forgotten meeting** ends by itself after 10 minutes without speech, or 4 hours of duration
(`listen.meeting_idle_minutes`, `listen.meeting_max_minutes`).

**Several readers.** Every `buddy watch --as <name>` has its own cursor on disk. A second session does not
consume the first one's events, and a monitor that expired receives what it missed when it comes back. `--peek`
looks without advancing.

Other controls: `buddy meeting start|stop`, `buddy pause`, `buddy resume`. The MCP server (`meeting_status`,
`get_live_transcript`, `stop_meeting`) sees meetings opened by the listener.

## Session mode

```sh
buddy start
```

- `Ctrl+Alt+Space`: ask for a panel update now. If the model is already working, the request is queued rather
  than interrupting, so research in flight is not thrown away.
- `Ctrl+Alt+M`: end the session.
- `buddy stop`, from another terminal: the same.

Each answer replaces the whole suggestion set instead of stacking; when a topic is settled, the panel empties.
Everything is also written to `suggestions.md`. With `ui.overlay: true`, suggestions also appear in an
always-on-top window.

Other commands:

```sh
buddy benchmark            # compare ASR models on a recording of your own
buddy config
buddy devices
buddy doctor
buddy hotkeys
buddy tool --list          # the read-only connectors the agent may call
buddy watch --as name      # listening-mode events since this name last read
```

## Privacy and consent

- **Recording is explicit.** Buddy starts idle. It records only when you ask, by voice or by command. Starting a
  recording through MCP additionally requires `BUDDY_ALLOW_MCP_START=true` and `confirmed=true` in the call.
- **Check the law and the room.** Recording rules differ by country and by employer. Get the participants'
  consent before you record them.
- **Only your microphone can command Buddy.** Someone on the call saying "hey buddy, merge it" is recorded and
  never obeyed. A transcript is treated as data, not as instructions.
- **Anything that leaves the machine takes two steps.** Posting, commenting, approving, merging, creating a
  ticket: the agent drafts it and acts only after you approve that exact text.
- **Nothing sensitive belongs in the repository.** Recordings, `config.yaml`, `.env` and ASR models are
  gitignored.

## How the brain works

With `llm_provider: claude-code`, every analysis is a headless Claude Code run:

| Decision | Why |
|---|---|
| runs in `claude_workdir` | that directory's `CLAUDE.md` describes your work; Buddy has no such knowledge |
| `--setting-sources project` | does not inherit user allow-rules, which often permit `git push` |
| `--permission-mode dontAsk` | nobody is watching to approve; whatever is not allowed is denied |
| `claude_allowed_tools` / `claude_disallowed_tools` | read-only; write verbs of your helpers blocked explicitly |
| no `ANTHROPIC_API_KEY` in the environment | the key would bill the API instead of using the subscription |
| native `claude.exe` on Windows | the npm `.cmd` shim truncates the system prompt at the first newline |
| `--session-id`, then `--resume` | one conversation per meeting; from the second turn the context is cached |

Denied calls show up as `tools: denied` and are recorded in `copilot.json`, so you can decide whether to widen a
rule. Two consecutive brain failures turn the panel red and the reason is stored — a week of meetings with no
suggestions once went unnoticed for exactly that reason.

**BigQuery and Datadog** reach the agent through `buddy tool`, which enforces the guards in code: `SELECT` only,
a mandatory dry run, a refusal above `bigquery_max_scan_gb`, and `--maximum_bytes_billed`. **Jira and Slack** use
your own helper scripts, allowed only in their read-only subcommands. Credentials that live in files and are
never exported are loaded through `env_files` in `config.yaml`.

**Latency and cost.** Measured on a working conversation: 86s on the first turn (cold start, with a Datadog
query) and 43s on the next (Jira and git). It is meant for positioning and for questions left hanging, not for
instant answers. `claude_model: sonnet` or `claude_effort: medium` bring it down. Nothing is billed per token,
but each run counts against your subscription limits; `copilot.json` carries a dollar estimate so you can follow
it. The model runs only when there is new speech, so silence costs nothing.

**Other providers.** `openai`, `anthropic` and `ollama` still work, with the older engine: preloaded context, a
project index and a background investigator. None of them knows you the way your own agent session does, and
`auto` never picks `claude-code` — spending the subscription is an explicit choice.

## MCP server and skills

The local MCP server exposes meeting data only, with bounded reads: status, live transcript, saved meetings,
search, copilot context and related earlier meetings. Project reading is left to the agent's native tools, which
keeps the access boundary clear.

Two skills are installed:

- `meeting-copilot-context` (Claude Code and Codex) — relate a live or saved meeting to code, docs, git and
  external research, on demand.
- `buddy-listener` (Claude Code) — make a session the brain behind the listener: arm the monitor, act on voice
  commands, suggest during meetings.

## What a meeting produces

Each meeting is a folder under `meetings/YYYY-MM-DD_HHMMSS/`:

```text
transcript.txt      readable transcript, written as each utterance lands
transcript.jsonl    the same, structured
suggestions.md      everything the panel suggested, with its reasoning
summary.md          the final report
metadata.json       start, end, ASR backend and subsystem failures
copilot.json        memory, current suggestions, usage, last error, denied calls, brain session id
```

`copilot.json` keeps `brain_session_id`: after the meeting, `claude --resume <id>` in `claude_workdir` reopens
the same conversation, with everything the agent read during the call. With `save_audio: false`, the default,
audio is discarded after processing.

## Architecture

```text
System audio -> platform capture -> bounded queue --+
                                                    +-> ASR -> event bus -> TUI / overlay / event log
Microphone   -> platform capture -> bounded queue --+                 +-> incremental storage
                                                                      +-> triggers / memory -> agent

Saved meetings <-> local MCP <-> your agent session <-> current project / docs / git / web
```

Capture, ASR, storage, UI and the model are isolated, so one failing subsystem does not take the session down.

## Limitations

- `ME` and `REMOTE` are physical sources, not people. There is no diarisation.
- The listening mode needs Claude Code; other agents can read `buddy watch` on request, but nothing wakes them.
- Suggestion quality during a live meeting has not been measured yet; that is the next test.
- Each update takes 40 to 90 seconds. The panel follows the meeting; it does not answer a question on the spot.
- Wake-phrase detection transcribes all microphone speech to decide, then discards it. A dedicated wake-word
  detector would avoid that and cost less CPU (about 8% at rest today).
- Permission rules match by command prefix. A helper invoked differently from the rule (`sh ~/bin/jira.sh`
  rather than `~/bin/jira.sh`) is denied, and shows up in `tool_denials`.
- No desktop app, system tray or signed installer yet.
- Calendar, meeting platform, Jira, GitHub and CRM integrations are not automatic.

## Development

```sh
python -m venv .venv
# Windows:     .\.venv\Scripts\python -m pip install -e ".[dev]"
# Linux/macOS: .venv/bin/python -m pip install -e ".[dev]"
pytest -q --cov --cov-fail-under=80
```

CI runs on `ubuntu-latest` (3.12 and 3.13), `windows-latest` and `macos-15`: tests with coverage, a compile check
of every module, an import check of the current platform's modules, a syntax check of the Swift helper in
`native/macos/MeetingAudioCapture.swift`, and a CLI smoke test.

The 80% coverage gate covers the hardware-independent core: copilot, memory, MCP, TUI, overlay, storage,
configuration and ASR selection. Audio capture, ASR, benchmark, `doctor`, CLI and session need real devices and
are verified by `buddy doctor` and the checklist in [docs/PORTABILITY.md](docs/PORTABILITY.md).

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for what is especially useful
(platform validation, support for other agents) and what will not be merged (anything that records without being
asked, or sends audio off the machine). Security reports go through the Security tab, as described in
[SECURITY.md](SECURITY.md).

The older `meeting-agent` and `meeting-agent-mcp` commands remain as compatibility aliases, as do the
`MEETING_AGENT_*` and `MEETING_COPILOT_ALLOW_MCP_START` environment variable names.

## License

[MIT](LICENSE).
