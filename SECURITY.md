# Security policy

Buddy runs with an open microphone and, in listening mode, hands voice commands to an agent that can read a
user's projects. Anything that breaks those boundaries matters. In particular:

- speech from a remote participant being acted on as a command, rather than recorded as data;
- a path that writes outside `meetings_dir`, or reads files the user did not allow;
- a way to start recording without the user asking, including through the MCP server;
- credentials, `.env` contents or transcripts leaking into logs, events or model prompts;
- the read-only guards in `buddy tool` (SELECT-only, dry run, byte ceiling) being bypassed.

## Reporting

Please report privately, not in a public issue: use **Report a vulnerability** on the repository's Security tab,
which opens a private advisory visible only to the maintainer.

Include what you did, what happened, and the version or commit. A proof of concept helps. You will get an
acknowledgement as soon as the maintainer sees it; this is a personal project, so expect days rather than hours.

## Scope

The audio path is local by design: transcription never leaves the machine. Reports that depend on the user
deliberately configuring a remote provider, or on an attacker who already has an account on the machine, are
usually not vulnerabilities — but send them anyway if you think the default is unsafe.
