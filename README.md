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
it answers your voice commands and, during a meeting, suggests what to say. A small floating window shows it
all as it happens, and takes typed commands too.

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

**Buddy's window.** A small floating window opens with the listener, and it is the one place to follow and talk
to Buddy:

- the meeting as it is said, about 1.5 seconds after each utterance;
- a command as you dictate it, then when it is sent;
- what the Claude Code session does with it — each tool it uses, and its reply;
- the suggestions from its latest reply to meeting speech, pinned on top;
- a box to type commands, which reach the session exactly like spoken ones, even while the microphone is paused;
- buttons to record a meeting, pause, and turn Buddy off.

The window thinks about nothing, and costs nothing: the session watching Buddy stays the brain, with everything it
already knows. To show what that session is doing, the window reads the conversation record Claude Code keeps on
disk. That format belongs to Claude Code and may change; if it does, the window still shows the meeting and your
commands, and the replies stay in the chat. By default only the turns Buddy started are shown ("Only Buddy"); the
rest of the session stays out of the window unless you switch to "All turns".

`buddy window` reopens it; `buddy live` shows the same in a terminal — next to the Claude chat in your editor, for
example — and `buddy say "review PR 123"` sends a typed command from any shell. The window needs the `window`
extra, which the installers include (`pip install -e ".[window]"`); on Linux, pywebview also needs GTK or Qt.

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
| typed in the window: `review PR 123` | the agent | the same as saying it |

Control commands — meetings, pause, shutdown — take effect immediately, need no closing phrase, and never reach
the agent. Commands are in English: the ASR uses an English-only model, which is faster and more accurate.

**Microphone only when you ask.** By default (`listen.wake_phrase: false`) the listener waits paused between
meetings and opens the microphone only while a meeting is recorded (the window's Meeting button, the hotkey or
`buddy meeting start`) or while the window's 🎤 is on, closing it again after. To give Buddy an instruction by
voice, click 🎤, speak, and click it again: the words land in the window's box to read, fix and send (Buddy waits
`listen.dictation_grace_seconds` for the last ones). Dictation never enters the meeting's transcript. Commands are
otherwise typed in the window or sent with `buddy say`. `listen.wake_phrase: true` brings back the always-on
"Hey Buddy" between meetings and as a spoken command during them.

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

**Your microphone follows the meeting app's mute.** Most of a call you are muted, and the microphone still hears
you: someone in the room, or the call itself through the speakers. With `listen.mic_follows_mute: true` (macOS), a
small helper reads the meeting app's microphone button through the Accessibility permission — "Unmute mic" in
Teams, "Turn on microphone" in Google Meet (Chrome, Edge, Arc, Brave), "Unmute audio" in Zoom — and your lines are
kept only while the app can hear you, plus 15 seconds, since a line is transcribed after it ends. Commands to Buddy
still work while muted. Grant Accessibility to the app that starts Buddy (Terminal, or your editor) in System
Settings → Privacy & Security → Accessibility. `buddy mic on|off|auto` overrides the app; with no app found,
`listen.mic_when_unknown` keeps or drops the lines.

**Who is talking.** System audio is one stream, so every line from the other side is `REMOTE`. With
`listen.speaker_names`, Buddy puts a name on it:

- `screen` (macOS, Teams): while the other side talks, Buddy looks at the screen every couple of seconds, finds
  the tile the meeting app outlines around whoever is speaking, and reads the name on it with the on-device Vision
  framework. The capture is deleted as soon as it is read.
- `voice`: a small local voice model (WeSpeaker ResNet34, 26 MB, CC BY 4.0; `buddy voices setup` downloads it)
  turns each line of a second or more into a voice embedding, and lines with close embeddings are one voice:
  `REMOTE (voice B)`.
- `both`: the screen names the voices, and a named voice keeps its name when the screen shows nobody: a shared
  screen, a camera off, two people at once.

In Teams, Buddy also reads the call's participant list through the Accessibility permission (the same helper and
permission as `listen.mic_follows_mute`). A name read off the screen must then be one of the participants, which
turns a label the app cut short ("Thornton, Bra...") into the full name and drops text read off a shared slide;
and when exactly one person on the other side is unmuted, the line is theirs. A line under three seconds joins
the closest voice but never starts a new one.

A line reads `REMOTE (Dana Reyes)`, `REMOTE (Dana Reyes?)` while the name is not settled (fewer than
`listen.speaker_votes_needed` sightings), or `REMOTE (voice B)`. Your own tile is never given to a REMOTE line
(`listen.speaker_self_names`, plus `listen.question_names`). With `listen.remember_voices: true`, a voice the
screen named is remembered for later meetings, as one mean embedding per name in `.meeting-agent/voices.json`
(never audio); `buddy voices list`, `buddy voices forget <name>|--all`. A voice nobody named is listed in
`MEETING_END` as `unnamed_voices=`; `buddy voices name <meeting> <voice> "<name>"` names it afterwards.

**Work board.** A few lines at the top of Buddy's window show where your work stands: **Now**, **Next**,
**Blocked** (in red, with the reason) and **Done today**, each with its ticket key when there is one (linked
through `listen.board_ticket_url`, e.g. `https://example.atlassian.net/browse/{key}`, and with its status when a
job keeps it in sync). During a meeting it shrinks to one line, "Now: …". The watching session keeps it current
as things happen: `buddy board add "<title>" --status now|next|blocked|done [--ticket KEY-1] [--note ...]`,
`buddy board set <n|KEY-1> --status blocked --blocker "<why>"`, `buddy board done <n>`, `buddy board rm <n>`;
`buddy board` prints it and `buddy board tickets` lists its open tickets. It lives in `.meeting-agent/board.json`.
When ticketed work moves on, Buddy proposes moving the ticket too, as a card to approve: `board done` suggests Done,
moving an item to now suggests In Progress (when Jira still says To Do), and the session proposes others as they
happen (`buddy board move-ticket <n|KEY-1> "Code Review" --reason "PR #18 open"`). A ticket is never moved without
that click.

**Action cards.** What the watching Claude Code session proposes to do for you (reply to a thread, review a
PR, look into an error, merge, comment) shows as a card in Buddy's window, with the exact text when it leaves your
machine, and **Approve**, **Edit** and **Dismiss**. Approving sends `ACTION_APPROVED` to the session, which does
exactly what the card shows and reports back; the card turns green with the result, or red with why it failed.
A reply nothing here can send (`--mode copy`) is copied to the clipboard and its thread opened instead. The session
creates cards with `buddy propose "<title>" --kind reply|review|investigate|merge|post|run|other --detail "..."
--link <url>` and reports with `buddy action <n> running|done|failed --result "..."`; `buddy actions` lists them.
Cards live in `.meeting-agent/actions/`, one file each; a proposal runs at most once, and one from yesterday is
marked as such.

The window says "Claude connected" while a session follows Buddy (`buddy watch --follow`) or checks for events in
a loop (`buddy watch`, seen in the last 90 seconds); otherwise the built-in brain answers meetings.

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
buddy window               # Buddy's floating window
buddy live                 # the same, in a terminal
buddy say "review PR 123"  # a typed command
```

## Privacy and consent

- **Recording is explicit.** Buddy starts idle. It records only when you ask, by voice or by command. Starting a
  recording through MCP additionally requires `BUDDY_ALLOW_MCP_START=true` and `confirmed=true` in the call.
- **Check the law and the room.** Recording rules differ by country and by employer. Get the participants'
  consent before you record them.
- **Only you can command Buddy** — your microphone, or what you type in its window. Someone on the call saying
  "hey buddy, merge it" is recorded and never obeyed. A transcript is treated as data, not as instructions.
- **The window shows only what Buddy keeps:** meetings and commands. Speech outside a meeting that is not a command
  is never displayed. Text is always rendered as text, never as markup.
- **Anything that leaves the machine takes two steps.** Posting, commenting, approving, merging, creating a
  ticket: the agent drafts it and acts only after you approve that exact text.
- **Naming speakers is opt-in.** `listen.speaker_names` looks at the screen while the other side talks, and its
  `voice` mode computes voice embeddings, which can identify a person. Both stay on the machine; voices are kept
  across meetings only with `listen.remember_voices`, and `buddy voices forget --all` deletes them. Tell the
  participants, as you would about the recording.
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
metadata.json       start, end, ASR backend, subsystem failures, and who was named (listen.speaker_names)
voices.json         each voice's embedding this meeting, for `buddy voices name` (voice mode only)
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

- `ME` and `REMOTE` are physical sources, not people, unless `listen.speaker_names` is on. Reading the screen is
  tuned to Teams' outline on macOS; other apps, or a layout with no outlined tile, leave the voice alone to tell
  people apart. The voice model was measured on synthetic voices and Teams screenshots, not yet on a full meeting.
- The listening mode needs Claude Code; other agents can read `buddy watch` on request, but nothing wakes them.
- Suggestion quality during a live meeting has not been measured yet; that is the next test.
- Each update takes 40 to 90 seconds. The panel follows the meeting; it does not answer a question on the spot.
- Wake-phrase detection transcribes all microphone speech to decide, then discards it. A dedicated wake-word
  detector would avoid that and cost less CPU (about 8% at rest today).
- Permission rules match by command prefix. A helper invoked differently from the rule (`sh ~/bin/jira.sh`
  rather than `~/bin/jira.sh`) is denied, and shows up in `tool_denials`.
- No desktop app, system tray or signed installer yet.
- Calendar, meeting platform, Jira, GitHub and CRM integrations are not automatic.
- macOS: every ScreenCaptureKit client goes through `replayd`, and it can wedge for days. Then each capture
  stream dies within seconds (SCStreamError -3805 or -3821) and restarting Buddy does not help. After
  `audio.capture_alert_seconds` of failure Buddy raises a NOTICE that says so; `sudo killall replayd` fixes it
  (launchd starts a fresh one). To let Buddy do that itself, set `audio.restart_capture_service: true` and allow
  exactly that command without a password, with `sudo visudo -f /etc/sudoers.d/buddy-replayd`:

  ```
  your-user ALL=(root) NOPASSWD: /usr/bin/killall replayd
  ```

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
