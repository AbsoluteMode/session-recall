# src/session_recall/retrieve.py
import json
import time
from pathlib import Path
from .store import Store
from .embed import Embedder
from .rerank import Reranker
from .models import Anchor, Turn
from .scope import repo_root, scope_clause
from .timefmt import humanize_ts

def _snippet(text: str, n: int = 200) -> str:
    return text[:n] + ("…" if len(text) > n else "")

class Recall:
    def __init__(self, store: Store, embedder: Embedder, reranker: "Reranker | None" = None):
        self.store = store
        self.embedder = embedder
        self.reranker = reranker

    @staticmethod
    def _anchor(c, score: float) -> Anchor:
        return Anchor(session_id=c.session_id, uuid=c.uuid, role=c.role,
                      snippet=_snippet(c.text), score=score, project=c.project, when=c.ts)

    def recall_search(self, query: str, k: int = 10, candidates: int = 100,
                      scope_cwd: str | None = None) -> list[Anchor]:
        # scope_cwd is the agent's raw cwd; normalize to a repo root so the main
        # checkout and every worktree under it collapse to one scope. None = global.
        root = repo_root(scope_cwd) if scope_cwd else None
        order: list[int] = []
        dist: dict[int, float | None] = {}
        try:
            qv = self.embedder.embed_query(query)
            for cid, d in self.store.knn(qv, candidates, scope_root=root):
                order.append(cid)
                dist[cid] = d
        except Exception:
            pass  # embedding unavailable -> FTS-only
        for cid in self.store.fts(query, candidates, scope_root=root):
            if cid not in dist:
                order.append(cid)
                dist[cid] = None  # keyword match, no vector distance
        if not order:
            return []
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
            return [self._anchor(chunk_by_id[distinct[idx]], score) for idx, score in ranked]
        # no reranker: KNN-similarity order; score monotonic in similarity, metric-agnostic
        out: list[Anchor] = []
        for cid in distinct[:k]:
            d = dist.get(cid)
            score = round(1.0 / (1.0 + d), 4) if isinstance(d, (int, float)) else 0.0
            out.append(self._anchor(chunk_by_id[cid], score))
        return out

    def _read_turns(self, file_path: str) -> list[dict]:
        turns: list[dict] = []
        try:
            with open(file_path, "rb") as f:
                for raw in f:
                    try:
                        turns.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            # The transcript was deleted/moved/made-unreadable since indexing. A
            # global grep touches EVERY indexed path, so one vanished file must not
            # abort the whole scan; expand_around/step degrade to [] (consistent
            # with their existing uuid-not-found path). WHY:
            # docs/decisions/2026-06-27-grep-resilient-to-deleted-transcripts.md
            return []
        return turns

    def _file_for(self, uuid: str) -> str:
        row = self.store.db.execute(
            "SELECT file_path FROM chunks WHERE uuid = ? LIMIT 1", (uuid,)).fetchone()
        if not row:
            raise KeyError(uuid)
        return row[0]

    @staticmethod
    def _render_content(obj: dict) -> str:
        """Human-readable content for a raw turn.

        NEVER emits the encrypted thinking *signature* or the full message
        envelope (usage/requestId/etc.) — those flooded the agent with kilobytes
        of base64 and metadata, making drill-down useless. Assistant blocks are
        flattened to readable text; tool calls/outputs are shown compactly.
        """
        msg = obj.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                parts.append(b.get("text", ""))
            elif bt == "thinking":
                th = (b.get("thinking") or "").strip()  # the TEXT, never b["signature"]
                if th:
                    parts.append(f"[thinking] {th}")
            elif bt == "tool_use":
                arg = json.dumps(b.get("input", {}), ensure_ascii=False)
                parts.append(f"[tool_use:{b.get('name', '')}] {arg[:300]}")
            elif bt == "tool_result":
                rc = b.get("content")
                rc = rc if isinstance(rc, str) else json.dumps(rc, ensure_ascii=False)
                parts.append(f"[tool_result] {rc[:500]}")
        return "\n".join(p for p in parts if p)

    def _as_turn(self, obj: dict) -> Turn:
        msg = obj.get("message") or {}
        return Turn(
            role=msg.get("role", ""),
            type=obj.get("type", ""),
            content=self._render_content(obj),
            raw={"uuid": obj.get("uuid", ""), "timestamp": obj.get("timestamp", "")},
        )

    def expand_around(self, session_id: str, uuid: str, before: int = 2, after: int = 2) -> list[Turn]:
        objs = self._read_turns(self._file_for(uuid))
        idx = next((i for i, o in enumerate(objs) if o.get("uuid") == uuid), None)
        if idx is None:
            return []
        lo, hi = max(0, idx - before), min(len(objs), idx + after + 1)
        return [self._as_turn(o) for o in objs[lo:hi]]

    def step(self, session_id: str, uuid: str, direction: str, count: int = 1) -> list[Turn]:
        objs = self._read_turns(self._file_for(uuid))
        idx = next((i for i, o in enumerate(objs) if o.get("uuid") == uuid), None)
        if idx is None:
            return []
        if direction == "next":
            target = idx + count
        elif direction == "prev":
            target = idx - count
        else:
            raise ValueError(f"direction must be 'next' or 'prev', got {direction!r}")
        if 0 <= target < len(objs):
            return [self._as_turn(objs[target])]
        return []

    def grep(self, pattern: str, session_id: str | None = None,
             scope_cwd: str | None = None) -> list[Anchor]:
        root = repo_root(scope_cwd) if scope_cwd else None
        clause, params = scope_clause("cwd", root)
        conds, args = [], []
        if session_id:
            conds.append("session_id = ?")
            args.append(session_id)
        if clause:
            conds.append(clause)
            args.extend(params)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = self.store.db.execute(
            "SELECT DISTINCT session_id, file_path, project FROM chunks" + where,
            tuple(args)).fetchall()
        hits: list[Anchor] = []
        for sid, path, project in rows:
            for o in self._read_turns(path):
                blob = json.dumps(o, ensure_ascii=False)
                if pattern in blob:
                    hits.append(Anchor(session_id=sid, uuid=o.get("uuid", ""), role=o.get("type", ""),
                                       snippet=_snippet(blob), score=1.0, project=project,
                                       when=0))
        return hits

    def recent_sessions(self, scope_cwd: str | None = None, limit: int = 10,
                        now: int | None = None) -> list[dict]:
        # Freshest sessions first — answers "what's the current state / how fresh is
        # the index" (the top entry's last_activity IS the effective freshness) and
        # surfaces the sessions of a thread spread across resume-created session_ids,
        # so the arc can be reassembled without manual sorting. now is injectable for
        # deterministic tests. WHY: docs/decisions/2026-06-27-recall-ergonomics-when-and-recent-sessions.md
        root = repo_root(scope_cwd) if scope_cwd else None
        now = int(time.time()) if now is None else now
        out: list[dict] = []
        for sid, project, last_ts, turns in self.store.recent_sessions(root, limit):
            out.append({
                "session_id": sid,
                "project": project,
                "turns": turns,
                "last_activity": last_ts,
                "last_activity_human": humanize_ts(last_ts, now),
                "label": _snippet(self.store.first_user_text(sid), 120),
            })
        return out
