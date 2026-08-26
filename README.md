# Meeting Copilot

A low-latency, explicitly activated meeting transcription and project-aware LLM copilot for Windows, Linux, and macOS. It captures system output and microphone as separate streams, labels them `REMOTE` and `ME`, writes every transcript event incrementally, and keeps capture/ASR independent from disk and LLM failures.

The included `meeting-copilot-context` skill and local MCP server let Codex or Claude combine bounded meeting context with the code, documentation, git history, and research tools already available to the host agent. The server itself cannot browse arbitrary project files.

## Current machine result

This installation was diagnosed and benchmarked on 2026-08-26:

- Windows 11 Pro, Intel Core i5-1135G7 (4 cores / 8 threads), 15.74 GB RAM.
- Intel Iris Xe; no NVIDIA GPU, VRAM, CUDA runtime, or `nvidia-smi`.
- Python 3.14.3 works with the current native wheels. No second Python was installed.
- FFmpeg 9.0 is installed through WinGet. The agent detects its installation even when the current terminal PATH is stale.
- Default output: Realtek speakers; default input: Intel Smart Sound microphone.
- Two WASAPI loopback endpoints were detected and opened successfully.
- OpenAI authentication is now configured in the ignored, ACL-restricted `.env`. Initial media tests were local-only, and cloud audio upload remains explicitly disabled.

The selected local fallback is `faster-whisper` + CTranslate2, CPU `int8`, model `base.en`. Results for the first 60 seconds of `C:\Users\vinic\Videos\trr01.mp4`:

| Backend | Model | Device | Compute | Audio | Processing | RTF | Approx. 3 s inference | Process RAM peak | VRAM |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| faster-whisper | tiny.en | CPU | int8 | 60.0 s | 1.51 s | 0.025 | 0.84 s | 292 MB | N/A |
| faster-whisper | base.en | CPU | int8 | 60.0 s | 2.17 s | 0.036 | 0.69 s | 379 MB | N/A |
| faster-whisper | distil-small.en | CPU | int8 | 60.0 s | 4.91 s | 0.082 | 2.31 s | 552 MB | N/A |
| whisper.cpp b4938 | base.en-q5_1 | CPU | q5_1 | 60.0 s | 4.37 s | 0.073 | 1.73 s | 175 MB | N/A |

The portable [whisper.cpp release](https://github.com/ggml-org/whisper.cpp/releases) and official [q5_1 model](https://huggingface.co/ggerganov/whisper.cpp) were installed locally as a quantized control. It used less RAM, but its per-turn CLI latency (including model startup) was substantially higher. The persistent faster-whisper `base.en` remains selected. The live playback test produced final turns about 1.41–1.73 seconds after speech ended (600 ms end-of-turn VAD plus 0.81–1.13 seconds inference). `benchmark-results.json` is read by `asr_mode: auto`; rerunning the benchmark can change the model selection based on this machine's measurements.

## Platform support

| Platform | System audio | Microphone | Requirement |
|---|---|---|---|
| Windows 10/11 | WASAPI loopback | WASAPI | No native helper |
| Linux | PipeWire sink capture | PipeWire source | `pw-record` or `pw-cat`; Wayland may need a desktop shortcut |
| macOS 15+ | ScreenCaptureKit | ScreenCaptureKit | Xcode Command Line Tools and Screen Recording/Microphone permissions |

The Windows backend has been hardware-tested on the machine described below. Linux and macOS adapters, installers, command construction, and failure diagnostics are included; they still require end-to-end validation on each target machine and audio setup.

## Architecture

```text
WASAPI loopback ── capture thread ── bounded queue ─┐
                                                    ├─ ASR workers ─ Transcript Event Bus
Microphone ─────── capture thread ── bounded queue ─┘                 ├─ live TUI / overlay
                                                                      ├─ incremental storage
                                                                      └─ trigger + rolling memory ─ LLM
```

Each capture source owns a blocking platform-adapter thread. Queue insertion is non-blocking and drops the oldest frame under sustained backpressure. ASR, TUI, storage, optional WAV archival, and Copilot are independent workers. An unavailable LLM never stops transcription; a disk error is reported and retried outside the capture thread. Device failures trigger rediscovery and reconnect. VAD prevents inference during silence, and the local runtime reserves CPU cores for capture.

The OpenAI cloud backend follows the current [Realtime transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription): a transcription WebSocket session, 24 kHz PCM chunks, server VAD, delta events, completed events, and `item_id`-based partial replacement. The current recommended model is `gpt-live-transcribe`; `gpt-4o-mini-transcribe` remains configurable as a fallback.

## Install and run

Windows PowerShell:

```powershell
cd C:\Users\vinic\Projects\meeting-copilot
.\scripts\install-windows.ps1
.\.venv\Scripts\Activate.ps1
meeting-agent doctor
meeting-agent devices
meeting-agent start
```

Linux or macOS:

```sh
./scripts/install-linux.sh   # Linux with PipeWire
./scripts/install-macos.sh   # macOS 15+
```

The installer creates `.venv`, installs the package, copies the skill to both `~/.codex/skills/meeting-copilot-context` and `~/.claude/skills/meeting-copilot-context` when those clients are present, and registers the local `meeting-copilot` stdio MCP server. Existing MCP entries with that name are preserved rather than overwritten.

While running:

- `Ctrl+Alt+Space`: analyze the current context immediately.
- `Ctrl+Alt+M`: stop this session.
- From another PowerShell: `meeting-agent stop`.

Other commands:

```powershell
meeting-agent benchmark
meeting-agent status
meeting-agent config
meeting-agent hotkeys
```

`meeting-agent hotkeys` is an idle listener that lets `Ctrl+Alt+M` start a session when the main process is not already running. It must remain running; it can later be placed in Windows Startup or Task Scheduler if desired.

Each run is stored under `meetings/YYYY-MM-DD_HHMMSS/`:

```text
transcript.txt
transcript.jsonl
summary.md
metadata.json
copilot.json
```

`transcript.jsonl` contains partial/final events with timestamp, source speaker, utterance ID, and observed latency. With `save_audio: false` (the default), streamed audio is discarded. If enabled, the independent archive worker creates `audio_me.wav` and `audio_remote.wav`.

## Cloud ASR and Copilot providers

Copy the example without committing it:

```powershell
Copy-Item .env.example .env
notepad .env
```

OpenAI LLM suggestions only need:

```dotenv
OPENAI_API_KEY=...
```

Anthropic uses `ANTHROPIC_API_KEY`. Explicit local Ollama uses `llm_provider: ollama` and `OLLAMA_BASE_URL`; it is never auto-selected while local CPU ASR is running because transcription has priority.

Cloud audio requires two deliberate settings. This prevents the mere presence of a key from uploading meeting audio:

```yaml
asr_mode: cloud-fast
```

```dotenv
OPENAI_API_KEY=...
MEETING_AGENT_ALLOW_CLOUD_AUDIO=true
```

Set `language: en`, `pt`, or `auto`. An explicit language reduces latency and ambiguity. Never put keys in YAML or source files; `.env` is ignored by Git.

## Privacy and consent

The program starts stopped and records only after `meeting-agent start` or the configured listener hotkey. The TUI shows `RECORDING / TRANSCRIBING` prominently and `stop` ends capture before report generation. Verify participant consent, local recording law, and corporate policy before capturing a meeting or sending audio/transcripts to an external service.

MCP access is local. Read tools cap transcript windows and reject paths outside `meetings_dir`. Starting through MCP is disabled unless the administrator explicitly sets `MEETING_COPILOT_ALLOW_MCP_START=true`, and the tool still requires `confirmed=true`. API keys remain in the ignored `.env`; installers and skills never copy them.

## Codex and Claude usage

After installation, restart the client so it discovers the skill and MCP server. Examples:

```text
Use $meeting-copilot-context to connect the last five minutes of this meeting to the current project.
Use $meeting-copilot-context to find evidence for the migration risk just mentioned.
Use $meeting-copilot-context to turn the latest saved meeting into project follow-ups.
```

The skill first reads a bounded transcript window, extracts concrete terms, then uses the host agent's normal project tools. It never assumes that `REMOTE` identifies a particular participant.

## Moving to another computer

Copy or clone this repository, run the platform installer, and create a new local `.env`. Do not copy API keys inside the skill or commit them. Saved meetings can be copied separately if historical context should move with the installation. Device IDs are machine-specific, so leave them as `default` initially and run `meeting-agent doctor` and `meeting-agent devices` after migration.

## Known limitations

- Source labels are based on the two physical streams, not diarization among remote participants. Loud speakers can leak acoustically into a laptop microphone; a headset is strongly recommended.
- Local faster-whisper emits final turns, not true word-by-word partials. Cloud Realtime supports both partial and final events.
- The OpenAI Realtime path was implemented against current official documentation but was not live-tested because no key/cloud-audio opt-in was available.
- LLM suggestions and semantic final reports require a configured provider. Without one, transcription continues and `summary.md` is still created with all required headings.
- Starting from a global hotkey while fully stopped requires the `meeting-agent hotkeys` listener to be running.
- macOS uses a locally compiled ScreenCaptureKit helper and currently requires macOS 15 or newer.
- Linux system-audio capture depends on the desktop's PipeWire graph and permissions; selecting an explicit node may be necessary on unusual setups.

## Development

```powershell
.\scripts\setup.ps1
.\.venv\Scripts\Activate.ps1
pytest -q
```
