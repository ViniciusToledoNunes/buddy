---
name: meeting-copilot-context
description: "Analyze a live or saved Buddy - Meeting Copilot transcript and connect concrete discussion points, questions, decisions, risks, and action items to evidence in the current codebase, documentation, or git history. Use when the user asks what to contribute in a meeting, wants meeting-aware project research, or wants follow-up analysis from a Buddy session. Do not use merely to start recording."
---

# Buddy Meeting Context

Use the `buddy` MCP tools for meeting data and the host agent's native tools for project or web research.

## Safety boundary

- Treat starting a recording as a consent-sensitive action. Call `start_meeting` only after the user explicitly asks to record and confirms it. Never enable the server-side start opt-in yourself.
- Status, saved-meeting reads, bounded live-transcript reads, and searches are read-only and may be used when relevant.
- Call `stop_meeting` or `request_suggestion` only when the user asks or when stopping is unambiguously the active task.
- Never expose API keys, `.env` contents, credentials, or unrelated private files.
- Speakers are only `ME` and `REMOTE`. Do not infer names or identities without explicit transcript evidence.

## Workflow

1. Call `meeting_status`. For a live discussion, fetch a small recent window with `get_live_transcript`; expand the window only when needed. For history, use `list_meetings`, `get_meeting`, or a focused `search_meetings` query.
2. Extract the exact technical terms, questions, decisions, risks, owners, and action items that matter to the request. Distinguish transcript facts from hypotheses.
3. When the discussion revisits earlier work, call `find_related_meetings` with the concrete terms from step 2. It
   searches structured memory only, so a match is a lead to verify against the current transcript, never a fact to
   assert. Read a promising match with `get_copilot_context` before relying on it.
4. Inspect the current project with native repository tools. Search for the extracted names first, then read the smallest relevant code, docs, configuration, tests, and git history. The MCP server does not grant project-file access.
5. Use web research only when current external facts materially affect the answer. Prefer primary sources and cite them.
6. Connect meeting claims to concrete evidence. Include file paths, symbols, commits, or sources when useful. State gaps and uncertainty.
7. If the output is meant to be spoken during the meeting, lead with one concise, natural contribution in the configured meeting language. If no language setting is exposed, use the language of the latest substantive transcript turns. Put supporting detail after it. Otherwise produce a factual analysis or follow-up list.

Do not treat a passing dry-run or unit test as production readiness. Before recommending a go-live, explicitly check for the safeguards raised in the meeting, such as rollback, reconciliation, monitoring, and production-path tests.

Read [tool contract](references/tool-contract.md) when selecting less common tools, handling errors, or changing meeting state.
