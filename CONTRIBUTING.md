# Contributing

Thanks for looking at Buddy. This is a small project with a clear shape, so the most useful thing you can do
before writing code is to open an issue describing what you want to change and why.

## What is especially welcome

- **Platform validation.** Linux and macOS capture backends are implemented but never tested end to end with
  real audio. A report saying what worked, what did not, and on which hardware is worth more than a patch.
- **Support for other agents.** The listening mode currently needs Claude Code, because it is the only agent
  with a way to wake a session when an event arrives. Adapters for Codex, Cursor, Copilot or Gemini CLI are the
  next milestone; see item 0 of [the roadmap](docs/ROADMAP.md). Deliver into the session the user already has
  open, rather than opening a window of Buddy's own.
- **A dedicated wake-word detector**, so speech that will be discarded is not transcribed at all.
- **Bug fixes**, especially anything that makes Buddy miss speech, misfire on the wake phrase, or fail silently.

## What will not be merged

- Recording that is hidden, automatic, or otherwise not asked for. Buddy starts idle and records when the user
  says so; that rule is not negotiable.
- Sending audio off the machine. Transcription is local, and there is no cloud ASR mode by design.
- Acting on the transcript as if it were instruction. A remote speaker saying "merge it" must never merge
  anything.
- Irreversible external actions without the user approving the exact content first.

## Setting up

```sh
python -m venv .venv
# Windows:     .\.venv\Scripts\python -m pip install -e ".[dev]"
# Linux/macOS: .venv/bin/python -m pip install -e ".[dev]"
pytest -q --cov --cov-fail-under=80
```

Python 3.12, 3.13 or 3.14. See [docs/PORTABILITY.md](docs/PORTABILITY.md) for the system prerequisites of each
platform, and run `buddy doctor` to check your machine.

## Sending a change

1. Keep it focused. One change per pull request, with a description of what it does and how you tested it.
2. Add tests. The suite is the reason this project can be changed safely: name tests after the behaviour they
   protect, and when you fix a bug, write the test that would have caught it.
3. Keep the coverage gate green (80%). CI runs the suite on Ubuntu (3.12 and 3.13), Windows and macOS, checks
   that every module compiles, that the current platform's modules import, that the Swift helper parses, and
   that the CLI starts.
4. Match the surrounding code: same naming, same comment density. Comments explain why, not what.
5. Never include recordings, transcripts, credentials, `.env` files, personal paths or identifiers — yours or
   anyone else's. Tests use fictional names.
6. If you touch audio capture, ASR or the platform backends, say which hardware and OS you tested on. CI cannot
   cover those.

## Reporting a bug

Include your OS and Python version, the output of `buddy doctor`, what you expected, and what happened. For
anything involving a meeting, please redact the transcript before pasting it.

Security issues: see [SECURITY.md](SECURITY.md). Please do not open a public issue for those.
