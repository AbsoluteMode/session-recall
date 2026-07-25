# src/session_recall/retrieve.py
import json
import time
from collections import deque
from .store import Store
from .embed import Embedder
from .rerank import Reranker
from .models import Anchor, Turn
from .scope import repo_root, in_scope, project_label
from .timefmt import humanize_ts
from .transcripts import (
    TranscriptEvent,
    is_navigable,
    iter_transcript_events,
    sanitize_raw,
)

def _snippet(text: str, n: int = 200) -> str:
    return text[:n] + ("…" if len(text) > n else "")


def _match_snippet(text: str, pattern: str, n: int = 240) -> str:
    """Return a bounded grep snippet centered near the actual match."""
    index = text.find(pattern)
    if index < 0:
        return _snippet(text, n)
    start = max(0, index - n // 3)
    end = min(len(text), start + n)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")

class SearchResult(list):
    """Anchors plus how they were found. Subclasses list so every existing caller
    keeps indexing and iterating unchanged; `degraded` carries the health note that
    the tool boundary turns into a warning for the agent."""

    def __init__(self, anchors=(), degraded: str | None = None):
        super().__init__(anchors)
        self.degraded = degraded


class Recall:
    def __init__(self, store: Store, embedder: Embedder, reranker: "Reranker | None" = None):
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        # Raw-only grep anchors have no chunks row. Keep an in-process hint so
        # the normal grep -> expand/step flow resolves without rescanning the
        # corpus; a streaming fallback below covers process restarts.
        self._anchor_files: dict[tuple[str, str, str], str] = {}

    @staticmethod
    def _validate_source(source: str | None) -> None:
        if source not in {None, "claude", "codex"}:
            raise ValueError(
                f"source must be 'claude', 'codex', or omitted; got {source!r}")

    @staticmethod
    def _anchor(c, score: "float | None") -> Anchor:
        return Anchor(session_id=c.session_id, uuid=c.uuid, role=c.role,
                      snippet=_snippet(c.text), score=score, project=c.project,
                      when=c.ts, source=c.source)

    def recall_search(self, query: str, k: int = 10, candidates: int = 100,
                      scope_cwd: str | None = None,
                      source: str | None = None,
                      start_ts: int | None = None,
                      end_ts: int | None = None) -> list[Anchor]:
        self._validate_source(source)
        # scope_cwd is the agent's raw cwd; normalize to a repo root so the main
        # checkout and every worktree under it collapse to one scope. None = global.
        root = repo_root(scope_cwd) if scope_cwd else None
        order: list[int] = []
        dist: dict[int, float | None] = {}
        degraded: str | None = None
        try:
            qv = self.embedder.embed_query(query)
            for cid, d in self.store.knn(
                    qv, candidates, scope_root=root, source=source,
                    start_ts=start_ts, end_ts=end_ts):
                order.append(cid)
                dist[cid] = d
        except Exception as exc:
            # WHY: docs/decisions/2026-07-26-voyage-403-egress-via-netcup.md
            # Embedding unavailable -> FTS-only. Never hard-fail: keyword hits still
            # beat nothing. But say so — a silent fallback looks identical to a
            # healthy search that found little, and the caller stops trusting recall
            # instead of fixing the embedder.
            degraded = (f"fts-only: embeddings unavailable, semantic ranking is off — "
                        f"only literal word matches are returned ({type(exc).__name__}: {exc})")
        for cid in self.store.fts(
                query, candidates, scope_root=root, source=source,
                start_ts=start_ts, end_ts=end_ts):
            if cid not in dist:
                order.append(cid)
                dist[cid] = None  # keyword match, no vector distance
        if not order:
            return SearchResult([], degraded)
        # Collapse exact duplicates (same content across resumed sessions / sidechains) so
        # identical text never wastes two top-k slots. All rows stay in the DB (provenance);
        # we keep the highest-priority occurrence (KNN order, then FTS).
        seen: set[str] = set()
        distinct: list[int] = []
        chunk_by_id = {}
        for cid in order:
            c = self.store.get_chunk(cid)
            if c.content_hash in seen:
                continue
            seen.add(c.content_hash)
            chunk_by_id[cid] = c
            distinct.append(cid)

        # Rerank if a reranker is configured AND reachable. The reranker is OPTIONAL (some
        # embedding providers ship none) and may be down — either way we fall back to the
        # KNN/FTS candidate order so recall never hard-fails.
        ranked: list[tuple[int, float]] | None = None
        if self.reranker is not None:
            try:
                ranked = self.reranker.rerank(
                    query, [chunk_by_id[cid].text for cid in distinct], top_k=k)
            except Exception:
                ranked = None

        if ranked is not None:
            return SearchResult(
                [self._anchor(chunk_by_id[distinct[idx]], score) for idx, score in ranked],
                degraded)
        # no reranker: KNN-similarity order; score monotonic in similarity, metric-
        # agnostic. Keyword-only hits carry None (no distance), not a fake 0.0 that
        # reads as "irrelevant" at the tool boundary.
        out: list[Anchor] = []
        for cid in distinct[:k]:
            d = dist.get(cid)
            score = round(1.0 / (1.0 + d), 4) if isinstance(d, (int, float)) else None
            out.append(self._anchor(chunk_by_id[cid], score))
        return SearchResult(out, degraded)

    def _files_for(self, uuid: str, session_id: str | None,
                   source: str | None = None) -> list[str]:
        """Transcript path candidates for an anchor. A uuid straight from
        recall_search resolves via chunks; grep anchors may point at raw turns
        that never became chunks (tool_result-only turns, filtered boilerplate),
        so fall back to the anchor's session: its chunk-bearing files, else the
        <session_id>.jsonl transcript itself (indexed but chunk-less)."""
        self._validate_source(source)
        files: list[str] = []
        exact_files: set[str] = set()
        for hinted_source in ((source,) if source else ("claude", "codex")):
            hinted = self._anchor_files.get((hinted_source, session_id or "", uuid))
            if hinted and hinted not in files:
                files.append(hinted)
                exact_files.add(hinted)

        source_sql = " AND source = ?" if source else ""
        uuid_params = (uuid, source) if source else (uuid,)
        uuid_files = [r[0] for r in self.store.db.execute(
            f"SELECT DISTINCT file_path FROM chunks WHERE uuid = ?{source_sql}",
            uuid_params).fetchall()]
        exact_files.update(uuid_files)
        files += [path for path in uuid_files if path not in files]
        if session_id:
            sid_params = (session_id, source) if source else (session_id,)
            files += [r[0] for r in self.store.db.execute(
                f"SELECT DISTINCT file_path FROM chunks WHERE session_id = ?{source_sql}",
                sid_params).fetchall() if r[0] not in files]
            escaped = (session_id.replace("\\", "\\\\")
                       .replace("_", "\\_").replace("%", "\\%"))
            indexed_source_sql = " AND source = ?" if source else ""
            indexed_params = ("%" + escaped + ".jsonl", source) if source else (
                "%" + escaped + ".jsonl",)
            files += [r[0] for r in self.store.db.execute(
                f"SELECT path FROM indexed_files WHERE path LIKE ? ESCAPE '\\'"
                f"{indexed_source_sql}", indexed_params).fetchall() if r[0] not in files]
        if files and not exact_files:
            # A resumed/mixed Claude transcript can carry a raw-only secondary
            # session while another file owns that session's surface chunks.
            # Try the cheap session/filename guesses first, then let _window
            # stream the remaining indexed files until the exact pair is found.
            indexed_source_sql = " WHERE source = ?" if source else ""
            indexed_params = (source,) if source else ()
            files += [row[0] for row in self.store.db.execute(
                f"SELECT path FROM indexed_files{indexed_source_sql}",
                indexed_params,
            ).fetchall() if row[0] not in files]
        if not files and session_id:
            # Compatibility fallback for a raw-only secondary Claude session
            # inside a mixed transcript whose filename belongs to another
            # session. Stream until the exact opaque uuid is found.
            indexed_source_sql = " WHERE source = ?" if source else ""
            indexed_params = (source,) if source else ()
            for path, path_source in self.store.db.execute(
                    f"SELECT path, source FROM indexed_files{indexed_source_sql}",
                    indexed_params).fetchall():
                try:
                    for event in iter_transcript_events(path, source=path_source):
                        if event.uuid == uuid and event.session_id == session_id:
                            files.append(path)
                            self._anchor_files[(path_source, session_id, uuid)] = path
                            break
                except OSError:
                    continue
                if files:
                    break
        if not files:
            raise LookupError(
                f"no indexed transcript for uuid={uuid!r} session_id={session_id!r} "
                f"(is the index fresh? run `session-recall index`)")
        return files

    @staticmethod
    def _as_turn(event: TranscriptEvent) -> Turn:
        """Render one normalized event without raw envelopes or encrypted fields."""
        return Turn(
            role=event.role,
            type=event.type,
            content=event.content,
            raw={"uuid": event.uuid, "timestamp": event.timestamp},
            source=event.source,
        )

    def _window(self, session_id: str, uuid: str, before: int, after: int,
                source: str | None) -> list[Turn] | None:
        """Stream to an anchor and retain only its bounded navigation window."""
        for path in self._files_for(uuid, session_id, source=source):
            path_source = source or self.store.indexed_source(path)
            previous: deque[Turn] = deque(maxlen=max(0, before))
            out: list[Turn] = []
            found = False
            remaining = max(0, after)
            try:
                for event in iter_transcript_events(path, source=path_source):
                    if not found:
                        if event.uuid == uuid and (
                                not session_id or event.session_id == session_id):
                            out = list(previous)
                            out.append(self._as_turn(event))
                            found = True
                            if remaining == 0:
                                return out
                        elif is_navigable(event):
                            previous.append(self._as_turn(event))
                        continue

                    if is_navigable(event):
                        out.append(self._as_turn(event))
                        remaining -= 1
                        if remaining == 0:
                            return out
            except OSError:
                # A transcript may be archived/deleted after indexing. Try any
                # other candidate and degrade to [] instead of crashing recall.
                continue
            if found:
                return out
        return None

    def expand_around(self, session_id: str, uuid: str, before: int = 2, after: int = 2,
                      source: str | None = None) -> list[Turn]:
        window = self._window(session_id, uuid, before, after, source)
        return window or []

    def step(self, session_id: str, uuid: str, direction: str, count: int = 1,
             source: str | None = None) -> list[Turn]:
        if count < 0:
            raise ValueError(f"count must be non-negative, got {count!r}")
        if direction == "next":
            window = self._window(session_id, uuid, 0, count, source)
            if window and len(window) == count + 1:
                return [window[-1]]
        elif direction == "prev":
            window = self._window(session_id, uuid, count, 0, source)
            if window and len(window) == count + 1:
                return [window[0]]
        else:
            raise ValueError(f"direction must be 'next' or 'prev', got {direction!r}")
        return []

    def grep(self, pattern: str, session_id: str | None = None,
             scope_cwd: str | None = None,
             source: str | None = None, limit: int = 100,
             start_ts: int | None = None,
             end_ts: int | None = None) -> list[Anchor]:
        self._validate_source(source)
        if limit < 1:
            return []
        root = repo_root(scope_cwd) if scope_cwd else None
        # Per-path chunk metadata. A file can carry SEVERAL (sid, cwd) pairs —
        # resumed sessions mix sessionIds in one transcript — so this is only a
        # fast-path skip (skip a file when NO row could match) and a fallback for
        # turns lacking their own fields; the authoritative filter is per turn.
        meta: dict[str, list[tuple[str, str, str, str]]] = {}
        for sid, path, project, cwd, chunk_source in self.store.db.execute(
                "SELECT DISTINCT session_id, file_path, project, cwd, source FROM chunks"
                ).fetchall():
            meta.setdefault(path, []).append((sid, project, cwd, chunk_source))
        # Scan ALL indexed transcripts, not just chunk-bearing ones: a file whose
        # every turn was filtered at extract (harness boilerplate, tool-only) is
        # exactly the under-the-hood content grep exists for.
        indexed = self.store.db.execute(
            "SELECT path, source FROM indexed_files").fetchall()
        paths = [(p, s or "claude") for p, s in indexed if not source or s == source]
        known = {p for p, _ in paths}
        paths += [(p, rows[0][3]) for p, rows in meta.items()
                  if p not in known and (not source or rows[0][3] == source)]
        hits: list[Anchor] = []
        for path, path_source in paths:
            rows = meta.get(path)
            if rows:
                # Codex files have one stable thread id. Claude resumed/mixed
                # files may contain raw-only secondary sessions absent from
                # chunks, so their authoritative filter stays per-event below.
                if (path_source == "codex" and session_id
                        and not any(s == session_id for s, _, _, _ in rows)):
                    continue
            try:
                # Strictly streaming: the largest local Codex rollout can be
                # hundreds of MB, and grep touches every indexed transcript.
                for event in iter_transcript_events(path, source=path_source):
                    if start_ts is not None or end_ts is not None:
                        if not event.ts:
                            continue
                        if start_ts is not None and event.ts < start_ts:
                            continue
                        if end_ts is not None and event.ts >= end_ts:
                            continue
                    t_sid = event.session_id or (rows[0][0] if rows else "")
                    if session_id and t_sid != session_id:
                        continue
                    t_cwd = event.cwd or (rows[0][2] if rows else "")
                    if root:
                        if t_cwd:
                            if not in_scope(t_cwd, root):
                                continue
                        else:
                            continue  # no cwd evidence -> not provably in scope
                    blob = json.dumps(sanitize_raw(event.obj), ensure_ascii=False)
                    if pattern in blob:
                        project = (project_label(t_cwd) if t_cwd
                                   else (rows[0][1] if rows else ""))
                        self._anchor_files[(event.source, t_sid, event.uuid)] = path
                        hits.append(Anchor(
                            session_id=t_sid,
                            uuid=event.uuid,
                            role=event.role or event.type,
                            snippet=_match_snippet(blob, pattern),
                            score=1.0,
                            project=project,
                            when=event.ts,
                            source=event.source,
                        ))
                        if len(hits) >= limit:
                            return hits
            except OSError:
                continue
        return hits

    def recent_sessions(self, scope_cwd: str | None = None, limit: int = 10,
                        now: int | None = None,
                        source: str | None = None,
                        start_ts: int | None = None,
                        end_ts: int | None = None) -> list[dict]:
        self._validate_source(source)
        # Freshest sessions first — answers "what's the current state / how fresh is
        # the index" (the top entry's last_activity IS the effective freshness) and
        # surfaces the sessions of a thread spread across resume-created session_ids,
        # so the arc can be reassembled without manual sorting. now is injectable for
        # deterministic tests. WHY: docs/decisions/2026-06-27-recall-ergonomics-when-and-recent-sessions.md
        root = repo_root(scope_cwd) if scope_cwd else None
        now = int(time.time()) if now is None else now
        out: list[dict] = []
        for row_source, sid, project, last_ts, turns in self.store.recent_sessions(
                root, limit, source=source, start_ts=start_ts, end_ts=end_ts):
            out.append({
                "session_id": sid,
                "source": row_source,
                "project": project,
                "turns": turns,
                "last_activity": last_ts,
                "last_activity_human": humanize_ts(last_ts, now),
                "label": _snippet(self.store.first_user_text(sid, row_source), 120),
            })
        return out
