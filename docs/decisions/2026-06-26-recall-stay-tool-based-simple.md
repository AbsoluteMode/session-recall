# session-recall stays tool-based and simple (distillation rejected)

**Date:** 2026-06-26

## Context

Goal: a memory so that Claude Code, when it starts a session and gets a task we've already
worked through, INSTANTLY gets up to speed by the most direct path — **via a tool, not
ambient**. I proposed a heavy plan: two prototypes (A — distilled "task
briefs", B — "smart arc") + a bake-off. Maxim hit the brakes: "this is getting too
complicated; I thought we'd just vectorize queries+answers and work on that."

## Decision

Stay on the current tool-based v1 (vectorize surface = user prompts + the assistant's text
answers) + targeted fixes. We do NOT build distillation / bake-off / ambient.

## Why (proven by a live test on the MCP tools)

- We played out a real "resume the task" (Drop silent-loss) on the live tools: `recall`
  found the right neighborhood; an `expand_around` dive into the "Итог" (Summary) turn returned a ready
  brief — decision + why + status + what's left.
- **Briefs ALREADY exist** in the data we vectorize — as past assistant "Итог" (Summary) answers.
  A separate distillation layer is not needed.
- The real blocker was a BUG: `expand_around` dumped ~5 KB of base64 (an encrypted
  thinking signature) + the full raw message envelope per turn → drill-down was useless.
  Fixed (`9e2b584`): clean rendering, 10000+ → 1534 characters.

## Rejected

- **Distilled "task briefs"** (a separate pipeline) — briefs already exist as
  "Итог" (Summary) turns; a pipeline = staleness + generation/maintenance cost.
- **A/B bake-off** — overengineering for a speculative gap.
- **Ambient auto-inject** (v2) — an on-demand tool is enough for now.
- **MMR** — exact duplicates are already solved by `content_hash` collapse.

## Remainder (parked — build only on a live signal)

- **#2 `recall_context(query)`** — one call = recall + auto-clean-expand → readable
  discussion blocks instead of snippets. NOT building it until real pain shows that
  `recall → expand` (both now clean) is not enough.
- v1.x: index subagent finals (`agent-*`, episode #28); summary-bias
  ranking (lift "Итог" (Summary)/decisive turns).

Principle: add complexity only when live usage proves the need.
