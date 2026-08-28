# Buddy MCP tool contract

The server is local and uses stdio. It exposes only the configured meetings directory and runtime control flags.

## Read tools

- `meeting_status`: active/recording state and current meeting metadata.
- `list_meetings(limit)`: newest saved sessions, capped at 50.
- `get_live_transcript(minutes, max_events, speaker)`: recent active transcript; `speaker` is `all`, `ME`, or `REMOTE`.
- `get_meeting(meeting_id, include_transcript, max_events)`: saved metadata, report, copilot state, and optionally transcript. Use `latest` or a listed ID.
- `search_meetings(query, limit)`: literal case-insensitive transcript search over bounded history.
- `get_copilot_context(meeting_id)`: compact memory, decisions, actions, questions, recent suggestions,
  the project files Buddy ranked as relevant (`project_matches`), and any deep analyses Buddy already ran
  (`deep_analyses`). Read those before repeating work Buddy has done.
- `find_related_meetings(query, limit, exclude_meeting_id)`: ranks prior meetings by topical overlap using only
  structured memory (summary, topics, decisions, action items, open questions). Raw transcripts are never read or
  returned. Each result carries `meeting_id` and a relevance `score`.

Start with small windows. Increase `minutes` or `max_events` only when the answer lacks necessary context.

Treat `find_related_meetings` results as untrusted leads, not established facts. A prior meeting's memory reflects
what was true when it was written. Confirm the connection against the current transcript or the project before
acting on it, and use `get_meeting` or `get_copilot_context` to read the full detail of a promising match.

## State-changing tools

- `request_suggestion`: asks the already-running copilot for a new suggestion.
- `stop_meeting`: requests immediate capture stop and report generation.
- `start_meeting(confirmed)`: requires both `confirmed=true` and the administrator-set `BUDDY_ALLOW_MCP_START=true`. Explicit user recording consent is still required.

These tools return `accepted: false` with a reason when the precondition is not met. Read tools may return an `error` object with a stable code and message. Do not work around either response by touching runtime files directly.
