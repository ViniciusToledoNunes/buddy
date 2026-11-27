# How to keep this memory

The user's working memory: what was done, what is in progress and what comes next, the decisions, the
people and the traps. More than one agent reads and writes here: Buddy (with whichever harness runs its
brain), and the user's own coding agents (Claude Code, Cursor, OpenCode). All of them follow these rules.

- `global/`: who the user is and how they want things done, in any project.
- `work/`: the current job: plan, projects, team, decisions, references.

## Reading

- Start with `global/MEMORY.md` and `work/MEMORY.md` (the indexes), then `work/project_open_items.md`
  (the current plan), then the files that touch the task.
- Memory records what was true when it was written. Check the code, the tracker or the chat before
  acting on it.

## When to write

- A decision, a fact about the environment, a trap someone fell into.
- A correction or preference from the user (feedback): it holds for every later session, of any agent.
- A change in the work: something finished, started, got blocked or was planned.

Never write what the code or git history already says, secrets (keys, tokens, the contents of `*.env`),
raw business data, or chatter that changes nothing later.

## Format

- One subject per file, `<type>_<subject>.md`, with type `user`, `feedback`, `project` or `reference`.
  Before creating one, look for a file on the same subject and update it instead.
- Front matter:

  ```
  ---
  name: <type>-<subject>
  description: <one line: what is here and when to read it>
  metadata:
    type: <type>
    modified: <ISO 8601 UTC>
    updated_by: <the agent: claude-code, cursor, opencode, buddy-cursor, buddy-claude-code...>
  ---
  ```

- Feedback: the rule, then **Why:** (what happened) and **How to apply:**.
- Absolute dates (2026-11-12), never "yesterday" or "next week".
- `work/project_open_items.md` is the plan (DOING / NEXT / WAITING ON / DONE): rewrite it, do not
  append. Old DONE items move to `work/project_open_items_archive_YYYY_MM.md`.

## Indexes

- Each `MEMORY.md` has one line per file: `- [Title](file.md) — one-line hook`.
- A new file gets a line; a deleted file loses its line.
- Stay under 200 lines: Claude Code only loads the first 200.

## More than one agent

- Reread a file right before editing it: another agent may have changed it.
- Edit the part that changed; do not rewrite what belongs to another subject. Do not delete a fact you
  cannot explain: mark it outdated and say why.
- This folder is a local git repository, committed automatically while Buddy runs. To undo, use
  `git revert`; never `reset`, `rebase`, `push`, or delete the history. It has no remote on purpose.
