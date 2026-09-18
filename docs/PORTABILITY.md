# Portability

What a new machine needs in order to run Buddy - Meeting Copilot.

## Shared requirements

- Python 3.12, 3.13 or 3.14.
- Git, to clone and update the repository.
- Internet during the first install and the first local-model download.
- About 2 GB free for the Python environment, caches and a basic local model. Larger models need more.
- An output device and a microphone the system recognises.
- FFmpeg is recommended, and required for the file-based benchmark; live capture does not depend on it on every
  backend.

A coding agent and API keys are optional. Without them Buddy still transcribes locally and saves meetings, but
there is no project research through the skill and no suggestions from a model.

## Windows 10/11

Requirements:

- PowerShell 5.1 or newer.
- 64-bit Python in a supported version; the installer tries 3.14, 3.13 and 3.12, in that order.
- A WASAPI output endpoint and a microphone.

Install and verify:

```powershell
.\scripts\install-windows.ps1
.\.venv\Scripts\Activate.ps1
buddy doctor
buddy devices
```

Windows is the platform currently validated on real hardware.

## Linux desktop

Requirements:

- PipeWire running in the user session.
- `pw-record` or `pw-cat` on the `PATH`.
- Python, and desktop support for whichever global hotkeys you want.
- `python3-venv`, `python3-dev` and a C compiler: on Linux `pynput` pulls in `evdev`, which publishes no wheels
  and has to be compiled. `install-linux.sh` checks for this before creating the venv.

Check first:

```sh
python3 --version
command -v pw-record || command -v pw-cat
```

Install:

```sh
./scripts/install-linux.sh
. .venv/bin/activate
buddy doctor
buddy devices
```

On Wayland, global hotkeys may have to be configured in the desktop environment itself. In unusual setups the
output PipeWire node may also need to be chosen explicitly.

`buddy doctor` runs a real, short capture on each stream, so `System audio` and `Microphone` turn `ok` only once
PipeWire actually delivered bytes. The REMOTE stream reads the default sink's monitor (`stream.capture.sink`),
so it captures what is *playing*; pick the right sink in `audio.system_device` if there is more than one.

Validated on Ubuntu 24.04 (PipeWire 1.0.5, X11, Python 3.12.3): ME and REMOTE are recorded in parallel and
transcribed separately. On Wayland `pynput` does not register the global hotkey; the session keeps recording and
the STATUS panel shows `hotkeys: warning`.

## macOS 15 or newer

Requirements:

- Python in a supported version.
- Xcode Command Line Tools, with `swiftc`.
- Screen Recording and Microphone permissions, plus Accessibility for global hotkeys.

Install:

```sh
xcode-select --install
./scripts/install-macos.sh
. .venv/bin/activate
buddy doctor
buddy devices
```

The installer compiles `native/macos/MeetingAudioCapture.swift` into `.venv/bin/meeting-audio-macos`. That binary
is not stored in Git, because it depends on the target platform.

## Moving between machines

Move:

- the Git repository;
- your own changes to `config.yaml`, reviewed for the new devices;
- optionally the `meetings/` folder, if the history should travel too.

Do not move through the repository:

- `.env` or API keys;
- `.venv`;
- caches and downloaded models;
- `.meeting-agent`, which holds transient state;
- device IDs, without validating them on the new machine.

## Acceptance checklist on a new machine

1. The installer finishes without errors.
2. `buddy doctor` reports no audio backend failure.
3. `buddy devices` lists the expected output and microphone.
4. A short session tells `ME` and `REMOTE` apart and ends with `buddy stop`.
5. `summary.md` and `transcript.jsonl` are created.
6. If an agent client is installed, the `buddy` MCP server connects and `meeting_status` answers.
7. If a remote provider is enabled, consent and opt-in were checked before the test.
