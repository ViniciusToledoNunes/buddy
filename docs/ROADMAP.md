# Roadmap

Buddy already covers capture, transcription, continuous suggestions, memory within and across meetings, reports,
and the connection to projects through MCP and skills. What follows would widen the product without touching the
central rule: recording is explicit.

## Delivered

- **Continuous suggestions.** Any new speech re-evaluates the whole set and replaces the panel; an empty set
  clears advice that stopped being useful. There is no keyword trigger any more.
- **Memory across meetings.** `find_related_meetings` ranks earlier meetings by term overlap over structured
  memory, available to the copilot during a meeting and to agents through MCP.
- **Cross-platform CI.** Tests with coverage on `ubuntu-latest` (3.12 and 3.13), `windows-latest` and `macos-15`,
  including a syntax check of the Swift helper and a per-platform import check.
- **Your own Claude Code as the brain.** The internal engine suggested without context: the "project" it received
  was Buddy's own code, and the model did not know the user. Every analysis is now a headless Claude Code run in
  the working directory, with the `CLAUDE.md`, the memory and the helpers already in daily use — one session per
  meeting, read-only, isolated from the user's permission rules. BigQuery and Datadog stay guarded in code,
  reached through `buddy tool`.
- **Listening mode.** `buddy listen` keeps the microphone attentive for "Hey Buddy", records meetings on request
  and publishes events to a log on disk; the `buddy-listener` skill makes a Claude Code session the brain, in the
  pattern of a chat monitor: a cursor on disk, a monitor, free investigation, and writes only after approval.
- **Commands that wait for you to finish.** A command opens with the wake phrase and is dispatched only on a
  closing phrase ("over and out"), with cancel phrases, a silence fallback marked as possibly incomplete, and
  tones for opened, sent and cancelled. Voice shutdown ends the process, and the watch exits with it.

## Next

### 0. Any agent, not only Claude Code

The listening mode depends on Claude Code's monitor tool to wake a session when an event arrives. No other agent
has an equivalent today (Codex has an open feature request, openai/codex#29922). The goal is for "Hey Buddy" to
work wherever the user already codes.

- Prefer routes that write into the session the user already has open: Codex's app-server, or a small editor
  extension that posts events into the chat. A window of Buddy's own is the last resort.
- Stream the transcript into the session as it is captured, not only batched suggestions.
- Keep one shared skill protocol with a short per-agent "turn on" section, and extend the installer to the other
  clients.

### 1. Listening mode

- Detect a meeting from the system signal: Windows reports which apps hold the microphone (Zoom, Chrome, Teams).
  Ask whether to record when a call starts unannounced, and end when the app releases the microphone. Needs an
  app list: a VDI client can hold the microphone all day.
- A dedicated wake-word detector, so speech that will be discarded is not transcribed at all.
- Validate "Hey Buddy" in real meetings: false-trigger rate, missed commands, and mistranscribed closing phrases.
  If closing fails often, add a hotkey that sends the open command.

### 2. Reduce brain latency

- It takes 40 to 90s per update today. Measure `claude_model: sonnet` and `claude_effort: medium` in a real
  meeting.
- End a forgotten session by itself, so it does not keep calling the model while the computer plays audio.
- Test the overlay (`ui.overlay: true`) as the main surface during calls: it already shows the current set, but
  the TUI sits in a terminal behind the meeting window.

### 3. Validate and package each platform

- End-to-end tests with real audio on Linux and macOS hardware. CI covers logic and imports, not capture.
- Signed installers and versioned releases.
- A tray app with an unambiguous recording indicator and the current suggestion set.

### 4. Understand participants, not just sources

- Diarisation of remote participants.
- Optional voice-to-name association, always confirmed by the user.
- Overlapping-speech detection and better separation of acoustic bleed.

### 5. Widen the useful context

- Move cross-meeting memory and the project index from BM25 to embeddings, so synonyms and paraphrases match
  (today `store` does not find `storage`).
- Encryption at rest for structured memory.
- Opt-in indexing of authorised projects and documentation.
- Pre-meeting preparation from the agenda, the participants and earlier meetings.

### 6. Close the execution loop

- Drafts of issues and tasks for GitHub, Jira, Linear or Todoist.
- Follow-up by email or chat, subject to human review.
- Documentation and ADR updates from approved decisions.
- Tracking of owners, deadlines and completed items.

## Further out

- A native desktop app with history, search and settings.
- Integration with Zoom, Google Meet, Microsoft Teams and calendars.
- Real-time translation and bilingual captions.
- A fully offline mode with a local LLM and GPU/NPU acceleration.
- Meeting sharing with encryption, retention and organisational controls.
- Meeting-quality metrics, such as decisions without an owner or risks without a plan.
- A stable local API and plugins for custom workflows.

## Out of scope

- Hidden or automatic recording without consent.
- Unrestricted sending of audio, transcripts or files to external services.
- Irreversible external actions without the user's review and authorisation.
- Silent inference of participants' identities.
