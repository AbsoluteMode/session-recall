"""Entry-per-file storage: the unit the agent's four verbs operate on.

Layout: ``<repo>/<project>/{bugs,actions,decisions}/<id>.md`` and the global
user map at ``<repo>/USER/<id>.md``. One file per entry because edit/delete
need an address: a category-wide file was fine when the model rewrote it
whole, but verbs demand ids. Frontmatter is a deliberately tiny dialect —
``key: value`` lines with JSON arrays for lists — so no YAML dependency and
no parser ambiguity.

The lexical search here serves the DISTILLER's dedup loop (search before
create): it must see an entry created seconds ago, so it reads the files
directly instead of waiting for the semantic index. Humans and working agents
get the semantic path via the main index (source="metadocs").
"""

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

CATEGORIES = ("bugs", "actions", "decisions", "user")
USER_DIR = "USER"

_FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_TOKEN_RE = re.compile(r"[a-zа-яё0-9_#-]{2,}", re.I)


@dataclass
class Entry:
    id: str
    project: str          # "" for the global user map
    category: str
    title: str
    body: str
    prs: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    created: str = ""
    updated: str = ""


def new_id(category: str) -> str:
    return f"{category[:3]}-{secrets.token_hex(3)}"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def entry_path(repo: Path, entry: Entry) -> Path:
    if entry.category == "user":
        return repo / USER_DIR / f"{entry.id}.md"
    return repo / entry.project / entry.category / f"{entry.id}.md"


def render(entry: Entry) -> str:
    front = "\n".join([
        f"id: {entry.id}",
        f"project: {entry.project}",
        f"category: {entry.category}",
        f"title: {entry.title}",
        f"prs: {json.dumps(entry.prs, ensure_ascii=False)}",
        f"sources: {json.dumps(entry.sources, ensure_ascii=False)}",
        f"created: {entry.created}",
        f"updated: {entry.updated}",
    ])
    return f"---\n{front}\n---\n\n{entry.body.strip()}\n"


def parse(text: str) -> Entry | None:
    m = _FRONT_RE.match(text)
    if not m:
        return None
    meta: dict = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        if not _:
            continue
        meta[key.strip()] = value.strip()
    try:
        return Entry(
            id=meta["id"], project=meta.get("project", ""),
            category=meta["category"], title=meta.get("title", ""),
            body=text[m.end():].strip(),
            prs=json.loads(meta.get("prs") or "[]"),
            sources=json.loads(meta.get("sources") or "[]"),
            created=meta.get("created", ""), updated=meta.get("updated", ""))
    except (KeyError, ValueError):
        return None


def save(repo: Path, entry: Entry) -> Path:
    entry.created = entry.created or _today()
    entry.updated = _today()
    path = entry_path(repo, entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(entry))
    return path


def load(repo: Path, entry_id: str) -> Entry | None:
    for path in repo.glob(f"*/*/{entry_id}.md"):
        got = parse(path.read_text())
        if got and got.id == entry_id:
            return got
    for path in repo.glob(f"{USER_DIR}/{entry_id}.md"):
        got = parse(path.read_text())
        if got and got.id == entry_id:
            return got
    return None


def delete(repo: Path, entry_id: str) -> bool:
    entry = load(repo, entry_id)
    if entry is None:
        return False
    entry_path(repo, entry).unlink(missing_ok=True)
    return True


def iter_entries(repo: Path, project: str | None = None,
                 category: str | None = None):
    globs = []
    if category == "user":
        globs = [f"{USER_DIR}/*.md"]
    elif project and category:
        globs = [f"{project}/{category}/*.md"]
    elif project:
        globs = [f"{project}/*/*.md", f"{USER_DIR}/*.md"]
    else:
        globs = ["*/*/*.md", f"{USER_DIR}/*.md"]
    seen = set()
    for pattern in globs:
        for path in sorted(repo.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            entry = parse(path.read_text())
            if entry:
                yield entry


def search(repo: Path, query: str, project: str = "",
           category: str | None = None, k: int = 8) -> list[tuple[float, Entry]]:
    """Term-overlap scoring over title+body, title hits weighted up. Lexical on
    purpose: the dedup search must see an entry created seconds ago, and the
    stories it deduplicates share literal anchors (error strings, PR numbers,
    file names) far more often than paraphrases."""
    terms = {t.lower() for t in _TOKEN_RE.findall(query)}
    if not terms:
        return []
    scored = []
    for entry in iter_entries(repo, project=project or None, category=category):
        title_tokens = {t.lower() for t in _TOKEN_RE.findall(entry.title)}
        body_tokens = {t.lower() for t in _TOKEN_RE.findall(entry.body)}
        score = 2.0 * len(terms & title_tokens) + len(terms & body_tokens)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda pair: -pair[0])
    return scored[:k]


# -- migration from the category-file format ----------------------------------
_OLD_FILES = ("bugs.md", "actions.md", "decisions.md")
_SECTION_RE = re.compile(r"^## +", re.M)
_SOURCES_RE = re.compile(r"^sources?:\s*(.+)$", re.I | re.M)


def needs_migration(repo: Path) -> bool:
    if (repo / "USER.md").exists():
        return True
    return any(p.name in _OLD_FILES for p in repo.glob("*/*.md"))


def _sections(text: str) -> list[tuple[str, str]]:
    """(title, body) per '## ' section; the preamble before the first section
    is dropped — it was only ever a file heading."""
    out = []
    for chunk in _SECTION_RE.split(text)[1:]:
        title, _, body = chunk.partition("\n")
        out.append((title.strip(), body.strip()))
    return out


def migrate(repo: Path) -> int:
    """Split every category file of the old format into entries and remove it.
    sources lines are carried over; ids are fresh. Returns entries created."""
    made = 0
    for old in list(repo.glob("*/*.md")):
        if old.name not in _OLD_FILES:
            continue
        project, category = old.parent.name, old.stem
        for title, body in _sections(old.read_text()):
            m = _SOURCES_RE.search(body)
            sources = [s.strip() for s in m.group(1).split(",")] if m else []
            body_clean = _SOURCES_RE.sub("", body).strip()
            save(repo, Entry(id=new_id(category), project=project,
                             category=category, title=title, body=body_clean,
                             sources=sources))
            made += 1
        old.unlink()
    user_map = repo / "USER.md"
    if user_map.exists():
        for title, body in _sections(user_map.read_text()) or [("Карта данных",
                                                               user_map.read_text())]:
            m = _SOURCES_RE.search(body)
            sources = [s.strip() for s in m.group(1).split(",")] if m else []
            save(repo, Entry(id=new_id("user"), project="", category="user",
                             title=title, body=_SOURCES_RE.sub("", body).strip(),
                             sources=sources))
            made += 1
        user_map.unlink()
    return made
