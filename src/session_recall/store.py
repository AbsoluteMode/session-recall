# WHY: pysqlite3 used instead of stdlib sqlite3 because the macOS system Python 3.13
# sqlite3 is compiled without SQLITE_ENABLE_LOAD_EXTENSION, so enable_load_extension()
# and load_extension() are absent. pysqlite3 (wheel) provides them, which sqlite-vec
# requires to load vec0. The rest of the API (execute, fetchall, etc.) is identical.
import sqlite3
if not hasattr(sqlite3.Connection, "enable_load_extension"):
    import pysqlite3 as sqlite3  # type: ignore[no-redef]  # macOS stdlib lacks extension loading
from pathlib import Path
import sqlite_vec
from .config import EMBED_DIM
from .models import Chunk
from .scope import scope_clause

_COLS = ["session_id", "uuid", "role", "text", "project", "cwd",
         "git_branch", "ts", "file_path", "byte_offset", "byte_len",
         "turn_index", "content_hash", "source"]
_INT_COLS = {"ts", "byte_offset", "byte_len", "turn_index"}


class Store:
    def __init__(self, db_path: Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path))
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self._schema()

    def _schema(self):
        col_defs = ", ".join(
            (f"{c} INTEGER" if c in _INT_COLS else
             "source TEXT NOT NULL DEFAULT 'claude'" if c == "source" else
             f"{c} TEXT")
            for c in _COLS
        )
        self.db.execute(f"CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, {col_defs})")
        # v0.3 adds transcript provenance without rebuilding the (potentially
        # very large) vector index. Existing rows are Claude Code history.
        chunk_cols = {row[1] for row in self.db.execute("PRAGMA table_info(chunks)")}
        if "source" not in chunk_cols:
            self.db.execute(
                "ALTER TABLE chunks ADD COLUMN source TEXT NOT NULL DEFAULT 'claude'")
        self.db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{EMBED_DIM}])")
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(text, chunk_id UNINDEXED)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS indexed_files("
            "path TEXT PRIMARY KEY, sig TEXT, source TEXT NOT NULL DEFAULT 'claude')")
        indexed_cols = {row[1] for row in self.db.execute("PRAGMA table_info(indexed_files)")}
        if "source" not in indexed_cols:
            self.db.execute(
                "ALTER TABLE indexed_files ADD COLUMN source TEXT NOT NULL DEFAULT 'claude'")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_source_session "
            "ON chunks(source, session_id)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_file_path ON chunks(file_path)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_ts ON chunks(ts)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_source_ts ON chunks(source, ts)")
        self.db.commit()

    def add(self, chunk: Chunk, embedding: "list[float] | bytes") -> int:
        """Insert one chunk. `embedding` is either a fresh vector or an already
        serialized float32 blob (reused verbatim from a previous index of the
        same content — see index.index_corpus). Writes are NOT committed here:
        the caller owns the transaction boundary (one commit per file), so a
        failure mid-file rolls back to the previous good state instead of
        leaving a half-indexed transcript. Store.close() commits pending work."""
        vals = [getattr(chunk, c) for c in _COLS]
        cur = self.db.execute(
            f"INSERT INTO chunks({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})", vals)
        cid = cur.lastrowid
        blob = embedding if isinstance(embedding, bytes) else sqlite_vec.serialize_float32(embedding)
        self.db.execute("INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, ?)", (cid, blob))
        self.db.execute("INSERT INTO fts_chunks(text, chunk_id) VALUES (?, ?)", (chunk.text, cid))
        return cid

    def embeddings_by_hash(self, path: str) -> dict[str, bytes]:
        """content_hash -> serialized embedding for a file's current rows.
        Snapshot taken BEFORE delete_file: transcripts are append-only, so on
        re-index most chunks are unchanged and their vectors are reused verbatim
        instead of re-embedding the whole file.
        WHY: docs/decisions/2026-07-02-post-review-hardening.md"""
        return {h: e for h, e in self.db.execute(
            "SELECT c.content_hash, v.embedding FROM chunks c "
            "JOIN vec_chunks v ON v.chunk_id = c.id WHERE c.file_path = ?", (path,))}

    def embeddings_by_session(self, session_id: str, source: str,
                              sig_prefix: str) -> dict[str, bytes]:
        """Reuse vectors when a transcript moves (for example into Codex's
        archive directory). Only rows whose file signature proves the current
        extractor/embedding space are eligible."""
        return {content_hash: embedding for content_hash, embedding in self.db.execute(
            "SELECT c.content_hash, v.embedding FROM chunks c "
            "JOIN vec_chunks v ON v.chunk_id = c.id "
            "JOIN indexed_files f ON f.path = c.file_path "
            "WHERE c.session_id = ? AND c.source = ? "
            "AND substr(f.sig, 1, ?) = ?",
            (session_id, source, len(sig_prefix), sig_prefix),
        )}

    def delete_file(self, path: str):
        """Remove all chunks (+ their vec/fts rows) for a file. Called before
        re-indexing a changed file so a growing transcript does not accumulate
        duplicate chunks every time it is re-scanned. No-op for a new file.
        Not committed here — part of the caller's per-file transaction.

        All three deletes share ONE criterion (file_path) resolved inside the
        write transaction. The previous version snapshotted ids first and then
        swept chunks by file_path: a parallel indexer — every concurrent Claude
        Code session fires the freshness hook — could commit rows for the same
        file in that window, and the sweep deleted their chunks row while their
        vec/fts rows, absent from the stale snapshot, survived as orphans.
        WHY: docs/decisions/2026-07-27-orphaned-index-rows.md"""
        for table in ("vec_chunks", "fts_chunks"):
            self.db.execute(
                f"DELETE FROM {table} WHERE chunk_id IN "
                "(SELECT id FROM chunks WHERE file_path = ?)", (path,))
        self.db.execute("DELETE FROM chunks WHERE file_path = ?", (path,))

    def prune_deleted(self, source: str | None = None) -> int:
        """Drop index rows for transcripts that no longer exist on disk.

        ``source`` limits reconciliation to one producer; this matters for a
        source-selective refresh. A deleted
        file is never re-visited by index_corpus (it only walks existing files), so
        without this its chunks linger forever — polluting recall_search results and
        (pre-resilience) crashing grep on open(). Returns the number of files pruned.
        WHY: docs/decisions/2026-06-27-grep-resilient-to-deleted-transcripts.md"""
        source_sql = " WHERE source = ?" if source else ""
        params = (source,) if source else ()
        gone = [r[0] for r in self.db.execute(
                f"SELECT path FROM indexed_files{source_sql}", params).fetchall()
                if not Path(r[0]).exists()]
        for path in gone:
            self.delete_file(path)  # chunks + vec + fts
            self.db.execute("DELETE FROM indexed_files WHERE path = ?", (path,))
        self.db.commit()
        return len(gone)

    @staticmethod
    def _filters(scope_root: str | None, source: str | None,
                 start_ts: int | None = None, end_ts: int | None = None,
                 *, alias: str = "c") -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        scope, scope_params = scope_clause(f"{alias}.cwd", scope_root)
        if scope:
            clauses.append(scope)
            params.extend(scope_params)
        if source:
            clauses.append(f"{alias}.source = ?")
            params.append(source)
        if start_ts is not None:
            clauses.append(f"{alias}.ts >= ?")
            params.append(start_ts)
        elif end_ts is not None:
            # Unknown timestamps are not evidence that a turn happened before
            # an upper bound, so exclude ts=0 from date-filtered queries.
            clauses.append(f"{alias}.ts > 0")
        if end_ts is not None:
            clauses.append(f"{alias}.ts < ?")
            params.append(end_ts)
        return " AND ".join(clauses), params

    def knn(self, query_vec: list[float], n: int, scope_root: str | None = None,
            source: str | None = None, start_ts: int | None = None,
            end_ts: int | None = None) -> list[tuple[int, float]]:
        clause, params = self._filters(scope_root, source, start_ts, end_ts)
        if not clause:
            # The IN-subquery is not a filter, it is the orphan guard: a vec row
            # whose chunk is gone is not a candidate. Scoped queries got this for
            # free from their prefilter; unscoped ones read vec_chunks straight and
            # handed dangling ids to get_chunk.
            # WHY: docs/decisions/2026-07-27-orphaned-index-rows.md
            rows = self.db.execute(
                "SELECT chunk_id, distance FROM vec_chunks "
                "WHERE embedding MATCH ? AND k = ? "
                "AND chunk_id IN (SELECT id FROM chunks) ORDER BY distance",
                (sqlite_vec.serialize_float32(query_vec), n)).fetchall()
            return [(r[0], r[1]) for r in rows]
        # sqlite-vec supports an IN-subquery prefilter on the vec0 primary key.
        # Keep the metadata predicate inside that subquery: filtering only
        # after a global top-k can starve a small source/repo, while asking for
        # the whole corpus exceeds sqlite-vec 0.1.9's k<=4096 limit.
        rows = self.db.execute(
            f"SELECT chunk_id, distance FROM vec_chunks "
            f"WHERE embedding MATCH ? AND k = ? "
            f"AND chunk_id IN (SELECT c.id FROM chunks c WHERE {clause}) "
            f"ORDER BY distance",
            (sqlite_vec.serialize_float32(query_vec), n, *params)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def fts(self, query: str, n: int, scope_root: str | None = None,
            source: str | None = None, start_ts: int | None = None,
            end_ts: int | None = None) -> list[int]:
        terms = [t for t in query.split() if t]
        if not terms:
            return []
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        clause, params = self._filters(scope_root, source, start_ts, end_ts)
        # ORDER BY rank (bm25, best first) — without it FTS5 returns rowid order
        # and LIMIT keeps an arbitrary oldest slice instead of the best matches.
        if not clause:
            # JOIN, not a plain scan: same orphan guard as knn above — an fts row
            # outliving its chunk must not become a candidate.
            rows = self.db.execute(
                "SELECT fts_chunks.chunk_id FROM fts_chunks "
                "JOIN chunks c ON c.id = fts_chunks.chunk_id "
                "WHERE fts_chunks MATCH ? ORDER BY fts_chunks.rank LIMIT ?",
                (match, n)).fetchall()
            return [r[0] for r in rows]
        # Scoped: filter applies BEFORE limit via JOIN — exact, no over-fetch.
        rows = self.db.execute(
            f"SELECT fts_chunks.chunk_id FROM fts_chunks "
            f"JOIN chunks c ON c.id = fts_chunks.chunk_id "
            f"WHERE fts_chunks MATCH ? AND {clause} ORDER BY fts_chunks.rank LIMIT ?",
            (match, *params, n)).fetchall()
        return [r[0] for r in rows]

    def get_chunk(self, chunk_id: int) -> "Chunk | None":
        """None when the chunk is gone. Absence is a normal outcome, not a bug: a
        background reindex deletes rows while a search is in flight, so the id a
        caller holds can die between candidate selection and this read."""
        row = self.db.execute(
            f"SELECT {', '.join(_COLS)} FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if row is None:
            return None
        data = dict(zip(_COLS, row))
        for k in ("ts", "byte_offset", "byte_len", "turn_index"):
            data[k] = int(data[k])
        return Chunk(**data)

    def recent_sessions(self, scope_root: str | None, limit: int,
                        source: str | None = None, start_ts: int | None = None,
                        end_ts: int | None = None) -> list[tuple]:
        """Sessions by most-recent activity (max ts), optionally scoped to a repo.
        Returns (source, session_id, project, last_ts, turns). Tiebreaks make
        equal-timestamp ordering deterministic."""
        clause, params = self._filters(
            scope_root, source, start_ts, end_ts, alias="chunks")
        where = f" WHERE {clause}" if clause else ""
        return self.db.execute(
            f"SELECT source, session_id, project, max(ts) AS last_ts, count(*) AS turns "
            f"FROM chunks{where} GROUP BY source, session_id "
            f"ORDER BY last_ts DESC, source, session_id LIMIT ?",
            (*params, limit)).fetchall()

    def first_user_text(self, session_id: str, source: str | None = None) -> str:
        """The session's earliest user prompt — a human label for the session."""
        source_sql = " AND source = ?" if source else ""
        params = (session_id, source) if source else (session_id,)
        row = self.db.execute(
            "SELECT text FROM chunks WHERE session_id = ? AND role = 'user' "
            f"{source_sql} ORDER BY turn_index LIMIT 1", params).fetchone()
        return row[0] if row else ""

    def mark_indexed(self, path: str, sig: str, source: str = "claude"):
        # Not committed here — joins the caller's per-file transaction, so the
        # "indexed" marker can never outlive a rolled-back set of chunks.
        self.db.execute(
            "INSERT INTO indexed_files(path, sig, source) VALUES (?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET sig = excluded.sig, source = excluded.source",
            (path, sig, source))

    def is_indexed(self, path: str, sig: str) -> bool:
        return self.stored_sig(path) == sig

    def stored_sig(self, path: str) -> "str | None":
        row = self.db.execute("SELECT sig FROM indexed_files WHERE path = ?", (path,)).fetchone()
        return row[0] if row else None

    def indexed_source(self, path: str) -> "str | None":
        row = self.db.execute(
            "SELECT source FROM indexed_files WHERE path = ?", (path,)).fetchone()
        return row[0] if row else None

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()

    def close(self):
        self.db.commit()  # no-op when nothing is pending; saves ad-hoc writers
        self.db.close()
