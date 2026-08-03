---
description: Recall past Claude Code, Codex, and Cursor sessions about a topic and return a decision-focused brief.
---

Recall the topic: "$ARGUMENTS" from the unified Claude Code + Codex + Cursor session index.

If the host exposes the dedicated `recall` subagent, dispatch it (in Claude Code:
`subagent_type: session-recall:recall`). Otherwise search iteratively with `recent_sessions`,
`recall_search`, `expand_around`/`step`, and `grep`.

Use a requested `claude`, `codex`, or `cursor` source filter; otherwise search all three. Return only a tight brief:
task, key decisions and why, tried/rejected approaches, current state, and
`source` + `session_id` + `uuid` pointers.

If "$ARGUMENTS" is empty, ask the user what to recall.
