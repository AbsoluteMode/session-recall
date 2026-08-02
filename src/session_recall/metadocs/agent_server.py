"""The distiller agent's whole world: four verbs over the entries store.

Runs as a stdio MCP server spawned per `claude -p` call (one call = one
session's dialogue), configured through environment variables:
METADOCS_REPO, METADOCS_PROJECT, METADOCS_SESSION.

Two invariants are MECHANICS here, not prompt requests:

- **search before create** — create() is rejected until search() has run for
  that category in this process. One process = one session's distillation, so
  the dedup check can never be skipped or forgotten, exactly what Maxim asked:
  «перед create всегда нужно использовать search».
- **no secrets in entries** — create/edit run the share secret scanner over
  title+body and refuse flagged text. The agent sees the reason and can mask
  the value; the repo may one day be pushed or shared, so the gate sits before
  the byte, not after.

delete() demands a reason (goes into the run log via the tool result) and is
git-revertible by construction — every run ends in one commit.
"""

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..share.scanner import scan
from . import entries
from .entries import CATEGORIES, Entry

mcp = FastMCP("metadocs")

_REPO = Path(os.environ.get("METADOCS_REPO", "."))
_PROJECT = os.environ.get("METADOCS_PROJECT", "")
_SESSION = os.environ.get("METADOCS_SESSION", "")
_searched: set[str] = set()


def _scan_error(title: str, body: str) -> dict | None:
    findings = scan(f"{title}\n{body}")
    if not findings:
        return None
    kinds = sorted({f.kind for f in findings})
    return {"error": f"secret detected ({', '.join(kinds)}) — entries must "
                     "name WHERE a secret lives, never its value. Rewrite "
                     "with the value masked."}


def do_search(query: str, category: str | None = None) -> list[dict]:
    _searched.add(category or "*")
    hits = entries.search(_REPO, query, project=_PROJECT, category=category)
    return [{"id": e.id, "category": e.category, "title": e.title,
             "prs": e.prs, "excerpt": e.body[:400]} for _, e in hits]


def do_create(category: str, title: str, body: str,
              prs: list[str] | None = None) -> dict:
    if category not in CATEGORIES:
        return {"error": f"unknown category {category!r}; use one of {CATEGORIES}"}
    if not (_searched & {"*", category}):
        return {"error": "search() first — dedup is mandatory: look for an "
                         "existing entry about this story, then create only "
                         "if nothing matched"}
    if not title.strip() or not body.strip():
        return {"error": "title and body must be non-empty"}
    err = _scan_error(title, body)
    if err:
        return err
    entry = Entry(id=entries.new_id(category),
                  project="" if category == "user" else _PROJECT,
                  category=category, title=title.strip(), body=body.strip(),
                  prs=list(prs or []),
                  sources=[_SESSION] if _SESSION else [])
    entries.save(_REPO, entry)
    return {"created": entry.id}


def do_edit(entry_id: str, body: str | None = None, title: str | None = None,
            add_prs: list[str] | None = None, append: str | None = None) -> dict:
    entry = entries.load(_REPO, entry_id)
    if entry is None:
        return {"error": f"no entry {entry_id!r} — search() to find the right id"}
    if body is not None:
        entry.body = body.strip()
    if append:
        entry.body = f"{entry.body}\n\n{append.strip()}".strip()
    if title is not None:
        entry.title = title.strip()
    for pr in add_prs or []:
        if pr not in entry.prs:
            entry.prs.append(pr)
    err = _scan_error(entry.title, entry.body)
    if err:
        return err
    if _SESSION and _SESSION not in entry.sources:
        entry.sources.append(_SESSION)
    entries.save(_REPO, entry)
    return {"updated": entry.id}


def do_delete(entry_id: str, reason: str) -> dict:
    if len(reason.strip()) < 10:
        return {"error": "give a real reason (≥10 chars) — deletions are audited"}
    if not entries.delete(_REPO, entry_id):
        return {"error": f"no entry {entry_id!r}"}
    return {"deleted": entry_id, "reason": reason.strip()}


@mcp.tool()
def search(query: str, category: str | None = None) -> list[dict]:
    """Find existing entries before creating new ones. Searches this project's
    bugs/actions/decisions and the global user map. ALWAYS search before
    create — create() refuses otherwise."""
    return do_search(query, category)


@mcp.tool()
def create(category: str, title: str, body: str,
           prs: list[str] | None = None) -> dict:
    """Create a new entry. category: bugs | actions | decisions | user.
    Only for a genuinely NEW story — update an existing entry via edit()
    when the story continues. prs: related pull requests / issues."""
    return do_create(category, title, body, prs)


@mcp.tool()
def edit(entry_id: str, body: str | None = None, title: str | None = None,
         add_prs: list[str] | None = None, append: str | None = None) -> dict:
    """Update an existing entry: replace body/title, append a paragraph, or
    attach PRs. Use this instead of create when the story already has an
    entry."""
    return do_edit(entry_id, body, title, add_prs, append)


@mcp.tool()
def delete(entry_id: str, reason: str) -> dict:
    """Remove an entry that is provably wrong or obsolete. Requires a real
    reason — it lands in the run log, and the removal is git-revertible."""
    return do_delete(entry_id, reason)


if __name__ == "__main__":
    mcp.run()
