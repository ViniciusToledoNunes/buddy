# Meeting Copilot MCP tool contract

The server is local and uses stdio. It exposes only the configured meetings directory and runtime control flags.

## Read tools

- `meeting_status`: active/recording state and current meeting metadata.
- `list_meetings(limit)`: newest saved sessions, capped at 50.
- `get_live_transcript(minutes, max_events, speaker)`: recent active transcript; `speaker` is `all`, `ME`, or `REMOTE`.
- `get_meeting(meeting_id, include_transcript, max_events)`: saved metadata, report, copilot state, and optionally transcript. Use `latest` or a listed ID.
- `search_meetings(query, limit)`: literal case-insensitive transcript search over bounded history.
- `get_copilot_context(meeting_id)`: compact memory, decisions, actions, questions, and recent suggestions.

Start with small windows. Increase `minutes` or `max_events` only when the answer lacks necessary context.

## State-changing tools

- `request_suggestion`: asks the already-running copilot for a new suggestion.
- `stop_meeting`: requests immediate capture stop and report generation.
- `start_meeting(confirmed)`: requires both `confirmed=true` and the administrator-set `MEETING_COPILOT_ALLOW_MCP_START=true`. Explicit user recording consent is still required.

These tools return `accepted: false` with a reason when the precondition is not met. Read tools may return an `error` object with a stable code and message. Do not work around either response by touching runtime files directly.
