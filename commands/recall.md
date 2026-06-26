---
description: Recall past Claude Code sessions about a topic — dispatches the deep-recall agent and returns a brief.
---

Dispatch the `recall` subagent (Agent tool, `subagent_type: session-recall:recall`) with the
topic: "$ARGUMENTS".

The agent searches past session history deeply and returns a tight brief (task, key decisions
and why, what was tried/rejected, current state, and anchor pointers). Relay that brief.

If "$ARGUMENTS" is empty, ask the user what to recall.
