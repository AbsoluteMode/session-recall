# Date-range recall uses inclusive local dates

## Context

Agents often need to answer calendar-shaped questions such as "what did I work on Tuesday?".
The index already stored epoch timestamps, but the MCP contract exposed only `scope_cwd` and
`source`. Writing a date into a semantic query did not constrain retrieval, and filtering tool
results client-side required broad scans that could miss sessions outside the first result page.

## Decision

`recall_search`, `recent_sessions`, and `grep` accept `on_date` for the common single-day case,
or optional inclusive `start_date` and `end_date` values in `YYYY-MM-DD` form, plus an optional
IANA `timezone` override. By default the MCP server derives the timezone from the user's computer.
Either range boundary may be omitted. Dates are converted once to a half-open epoch range
`[start, end)` using `zoneinfo`, so daylight-saving transitions are handled by local calendar
boundaries.

Indexed retrieval applies the range inside the SQLite metadata prefilter used by KNN, FTS, and
session aggregation. Raw grep applies the same range per normalized transcript event. Turns with
unknown timestamps are excluded whenever a date filter is active.

## Consequences

- A local day has identical semantics across semantic search, keyword search, and session lists.
- Filtering happens before candidate limits, avoiding false negatives from client-side trimming.
- Timestamp indexes keep date-scoped metadata queries cheap on large histories.
- `expand_around` and `step` remain cursor operations and intentionally do not accept a range.
