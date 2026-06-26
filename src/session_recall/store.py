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
         "turn_index", "content_hash"]
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
        col_defs = ", ".join(f"{c} INTEGER" if c in _INT_COLS else f"{c} TEXT" for c in _COLS)
        self.db.execute(f"CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, {col_defs})")
        self.db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{EMBED_DIM}])")
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(text, chunk_id UNINDEXED)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS indexed_files(path TEXT PRIMARY KEY, sig TEXT)")
        self.db.commit()

    def add(self, chunk: Chunk, embedding: list[float]) -> int:
        vals = [getattr(chunk, c) for c in _COLS]
        cur = self.db.execute(
            f"INSERT INTO chunks({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})", vals)
        cid = cur.lastrowid
        self.db.execute("INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, ?)",
                        (cid, sqlite_vec.serialize_float32(embedding)))
        self.db.execute("INSERT INTO fts_chunks(text, chunk_id) VALUES (?, ?)", (chunk.text, cid))
        self.db.commit()
        return cid

    def delete_file(self, path: str):
        """Remove all chunks (+ their vec/fts rows) for a file. Called before
        re-indexing a changed file so a growing transcript does not accumulate
        duplicate chunks every time it is re-scanned. No-op for a new file."""
        ids = [r[0] for r in self.db.execute(
            "SELECT id FROM chunks WHERE file_path = ?", (path,)).fetchall()]
        if ids:
            marks = ",".join("?" * len(ids))
            self.db.execute(f"DELETE FROM vec_chunks WHERE chunk_id IN ({marks})", ids)
            self.db.execute(f"DELETE FROM fts_chunks WHERE chunk_id IN ({marks})", ids)
            self.db.execute("DELETE FROM chunks WHERE file_path = ?", (path,))
        self.db.commit()

    def knn(self, query_vec: list[float], n: int, scope_root: str | None = None) -> list[tuple[int, float]]:
        clause, params = scope_clause("c.cwd", scope_root)
        if not clause:
            rows = self.db.execute(
                "SELECT chunk_id, distance FROM vec_chunks "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (sqlite_vec.serialize_float32(query_vec), n)).fetchall()
            return [(r[0], r[1]) for r in rows]
        # Scoped: vec0 KNN can't pre-filter on a joined column, so over-fetch
        # candidates then filter to the repo, keeping the top n. The brute-force
        # scan cost is independent of k, so over-fetching is ~free; clamp so a
        # repo that is a tiny slice of the corpus still gets enough survivors.
        total = self.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        k_over = min(total, max(300, min(n * 30, 2000)))
        rows = self.db.execute(
            f"SELECT vec_chunks.chunk_id, vec_chunks.distance FROM vec_chunks "
            f"JOIN chunks c ON c.id = vec_chunks.chunk_id "
            f"WHERE vec_chunks.embedding MATCH ? AND k = ? AND {clause} "
            f"ORDER BY vec_chunks.distance LIMIT ?",
            (sqlite_vec.serialize_float32(query_vec), k_over, *params, n)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def fts(self, query: str, n: int, scope_root: str | None = None) -> list[int]:
        terms = [t for t in query.split() if t]
        if not terms:
            return []
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        clause, params = scope_clause("c.cwd", scope_root)
        if not clause:
            rows = self.db.execute(
                "SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH ? LIMIT ?",
                (match, n)).fetchall()
            return [r[0] for r in rows]
        # Scoped: filter applies BEFORE limit via JOIN — exact, no over-fetch.
        rows = self.db.execute(
            f"SELECT fts_chunks.chunk_id FROM fts_chunks "
            f"JOIN chunks c ON c.id = fts_chunks.chunk_id "
            f"WHERE fts_chunks MATCH ? AND {clause} LIMIT ?",
            (match, *params, n)).fetchall()
        return [r[0] for r in rows]

    def get_chunk(self, chunk_id: int) -> Chunk:
        row = self.db.execute(
            f"SELECT {', '.join(_COLS)} FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        data = dict(zip(_COLS, row))
        for k in ("ts", "byte_offset", "byte_len", "turn_index"):
            data[k] = int(data[k])
        return Chunk(**data)

    def mark_indexed(self, path: str, sig: str):
        self.db.execute(
            "INSERT INTO indexed_files(path, sig) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET sig = excluded.sig", (path, sig))
        self.db.commit()

    def is_indexed(self, path: str, sig: str) -> bool:
        row = self.db.execute("SELECT sig FROM indexed_files WHERE path = ?", (path,)).fetchone()
        return row is not None and row[0] == sig

    def close(self):
        self.db.close()
