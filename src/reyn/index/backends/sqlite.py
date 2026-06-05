"""SqliteIndexBackend — per-source SQLite index with numpy cosine similarity.

Storage layout: <workspace_root>/.reyn/index/<source>/index.db
WAL mode enabled for concurrent readers.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Literal

from reyn.index.backend import ChunkRecord, DropResult, StatResult, WriteResult

if TYPE_CHECKING:
    import numpy as np  # pragma: no cover

# Fields from ChunkMetadata that may appear in SQL filter expressions.
# Restricting to these prevents arbitrary SQL column injection.
_ALLOWED_FILTER_FIELDS: frozenset[str] = frozenset(
    {"source_path", "source_type", "embedding_model", "parent_context"}
)

_DDL = """\
CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash    TEXT    UNIQUE NOT NULL,
    text            TEXT    NOT NULL,
    vector          BLOB    NOT NULL,
    metadata_json   TEXT    NOT NULL,
    source_path     TEXT    NOT NULL,
    source_type     TEXT    NOT NULL,
    embedding_model TEXT    NOT NULL,
    chunk_index     INTEGER NOT NULL,
    size_tokens     INTEGER NOT NULL,
    parent_context  TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_path     ON chunks(source_path);
CREATE INDEX IF NOT EXISTS idx_embedding_model ON chunks(embedding_model);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_BATCH_SIZE = 500


def _db_path(workspace_root: Path, source: str) -> Path:
    return workspace_root / ".reyn" / "index" / source / "index.db"


def _within_paths(path: Path, roots: "list[str]") -> bool:
    """True if ``path`` is one of ``roots`` or a descendant (resolved). #1199
    S3.4 Part1 — the sandbox write_paths cap check for the host-direct index
    write. Mirrors the resolved-path-under match in permissions/effective.py
    (replicated here so reyn.index does not depend on reyn.permissions)."""
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        return False
    for root in roots:
        try:
            r = Path(root).expanduser().resolve()
        except Exception:
            continue
        if p == r:
            return True
        try:
            p.relative_to(r)
            return True
        except ValueError:
            continue
    return False


def _open_db(db_file: Path) -> sqlite3.Connection:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    conn.commit()
    return conn


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _vec_to_blob(vector: list[float]) -> bytes:
    import numpy as np  # noqa: PLC0415

    return np.asarray(vector, dtype=np.float32).tobytes()


def _blob_to_vec(blob: bytes) -> "np.ndarray":
    import numpy as np  # noqa: PLC0415

    return np.frombuffer(blob, dtype=np.float32)


class SqliteIndexBackend:
    """SQLite-backed index backend (ADR-0033 Phase 1).

    Each logical source maps to an isolated SQLite database file at
    <workspace_root>/.reyn/index/<source>/index.db.

    Args:
        workspace_root: Root directory of the Reyn workspace.  Defaults to
            ``Path.cwd()`` so tests can instantiate without explicit wiring.
        sandbox_write_paths: #1199 S3.4 Part1 — when set (the phase sandbox
            policy's ``write_paths``, forwarded into the safe-mode subprocess
            where this backend's write bypasses ``require_file_*``), ``write``
            self-gates the DB path against the cap before opening it. ``None`` =
            no sandbox cap (in-process callers, which are already gated at the
            op layer). The SQLite I/O itself stays host-direct (random-access /
            lock cannot go on the read_file/write_file abstraction); only the
            path is checked, before ``sqlite3.connect``.
    """

    def __init__(
        self,
        workspace_root: Path | None = None,
        *,
        sandbox_write_paths: "list[str] | None" = None,
    ) -> None:
        self._root = workspace_root if workspace_root is not None else Path.cwd()
        self._sandbox_write_paths = sandbox_write_paths

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------

    async def write(
        self,
        source: str,
        chunks: Iterable[ChunkRecord],
        mode: Literal["append", "replace"],
    ) -> WriteResult:
        db_file = _db_path(self._root, source)
        # #1199 S3.4 Part1: SandboxLayer ∩ for the host-direct index write —
        # gate the DB path against the phase sandbox write_paths cap BEFORE the
        # connection opens (mirrors S3.1c-2's _path_under restrict-only).
        if self._sandbox_write_paths is not None and not _within_paths(
            db_file, self._sandbox_write_paths
        ):
            raise PermissionError(
                f"index write to {str(db_file)!r} denied by the active sandbox "
                f"policy (path outside write_paths={self._sandbox_write_paths!r}). "
                f"This is a sandbox restriction on the OS's host-direct index "
                f"write. Adjust the phase default_sandbox_policy write_paths if "
                f"the index should be writable here."
            )
        conn = _open_db(db_file)
        written = 0
        skipped = 0
        first_embedding_model: str | None = None

        try:
            with conn:  # single transaction
                if mode == "replace":
                    conn.execute("DELETE FROM chunks")
                    conn.execute("DELETE FROM meta")

                batch: list[tuple] = []

                def _flush(batch: list[tuple]) -> tuple[int, int]:
                    w = s = 0
                    for row in batch:
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO chunks "
                            "(content_hash, text, vector, metadata_json, "
                            " source_path, source_type, embedding_model, "
                            " chunk_index, size_tokens, parent_context) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            row,
                        )
                        if cur.rowcount > 0:
                            w += 1
                        else:
                            s += 1
                    return w, s

                for chunk in chunks:
                    meta: dict = chunk["metadata"]
                    content_hash: str = meta.get("content_hash", "")
                    em: str = meta.get("embedding_model", "")
                    if first_embedding_model is None and em:
                        first_embedding_model = em

                    row = (
                        content_hash,
                        chunk["text"],
                        _vec_to_blob(chunk["vector"]),
                        json.dumps(meta, ensure_ascii=False),
                        meta.get("source_path", ""),
                        meta.get("source_type", "generic"),
                        em,
                        meta.get("chunk_index", 0),
                        meta.get("size_tokens", 0),
                        meta.get("parent_context"),
                    )
                    batch.append(row)

                    if len(batch) >= _BATCH_SIZE:
                        w, s = _flush(batch)
                        written += w
                        skipped += s
                        batch = []

                if batch:
                    w, s = _flush(batch)
                    written += w
                    skipped += s

                # Update meta table
                now = _now_iso()
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed', ?)",
                    (now,),
                )
                if first_embedding_model is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES ('embedding_model', ?)",
                        (first_embedding_model,),
                    )
        finally:
            conn.close()

        return WriteResult(written=written, skipped=skipped)

    # ------------------------------------------------------------------
    # existing_hashes (pre-embed resume key)
    # ------------------------------------------------------------------

    async def existing_hashes(self, source: str) -> set[str]:
        """Return the set of ``content_hash`` values already indexed for *source*.

        This is the **pre-embed** resume key (#1303 Stage I): the caller
        skips chunks whose hash is already present *before* embedding, so a
        re-run or crash-resume does not re-embed already-indexed content (=
        the cost-save). It is distinct from :meth:`write`'s ``INSERT OR
        IGNORE``, which dedups *after* the vector has already been computed
        (no cost save). Returns an empty set when the source DB is absent.
        """
        db_file = _db_path(self._root, source)
        if not db_file.exists():
            return set()
        conn = sqlite3.connect(str(db_file), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            rows = conn.execute("SELECT content_hash FROM chunks").fetchall()
        finally:
            conn.close()
        return {r[0] for r in rows if r[0]}

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------

    async def query(
        self,
        source: str,
        query_vector: list[float],
        top_k: int,
        filters: dict[str, str],
    ) -> list[ChunkRecord]:
        db_file = _db_path(self._root, source)
        if not db_file.exists():
            return []

        conn = sqlite3.connect(str(db_file), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")

        try:
            # Build parameterized WHERE clause from allowed filter fields only.
            where_clauses: list[str] = []
            params: list[str] = []
            for field, value in filters.items():
                if field not in _ALLOWED_FILTER_FIELDS:
                    # Silently skip unknown filter fields to stay P7-clean.
                    continue
                where_clauses.append(f"{field} = ?")
                params.append(value)

            sql = "SELECT text, vector, metadata_json FROM chunks"
            if where_clauses:
                sql += " WHERE " + " AND ".join(where_clauses)

            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        if not rows:
            return []

        import numpy as np  # noqa: PLC0415

        texts = [r[0] for r in rows]
        vectors = np.stack([_blob_to_vec(r[1]) for r in rows])  # (N, D)
        metas = [json.loads(r[2]) for r in rows]

        q_vec = np.asarray(query_vector, dtype=np.float32)

        # Cosine similarity: dot / (|row| * |q|)
        q_norm = float(np.linalg.norm(q_vec))
        if q_norm == 0.0:
            scores = np.zeros(len(rows), dtype=np.float32)
        else:
            row_norms = np.linalg.norm(vectors, axis=1)
            # Avoid division by zero for zero vectors
            row_norms = np.where(row_norms == 0.0, 1e-10, row_norms)
            scores = (vectors @ q_vec) / (row_norms * q_norm)

        # Descending sort, take top_k
        top_indices = np.argsort(scores)[::-1][:top_k]

        results: list[ChunkRecord] = []
        for idx in top_indices:
            results.append(
                ChunkRecord(
                    text=texts[idx],
                    vector=vectors[idx].tolist(),
                    metadata=metas[idx],
                    score=float(scores[idx]),
                )
            )
        return results

    # ------------------------------------------------------------------
    # drop
    # ------------------------------------------------------------------

    async def drop(self, source: str) -> DropResult:
        source_dir = self._root / ".reyn" / "index" / source
        if not source_dir.exists():
            return DropResult(removed=False, chunks_dropped=0)

        db_file = source_dir / "index.db"
        count = 0
        if db_file.exists():
            conn = sqlite3.connect(str(db_file), check_same_thread=False)
            try:
                row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
                count = row[0] if row else 0
            finally:
                conn.close()

        shutil.rmtree(source_dir)
        return DropResult(removed=True, chunks_dropped=count)

    # ------------------------------------------------------------------
    # stat
    # ------------------------------------------------------------------

    async def stat(self, source: str) -> StatResult:
        db_file = _db_path(self._root, source)
        if not db_file.exists():
            return StatResult(chunk_count=0, embedding_model=None, last_indexed=None)

        conn = sqlite3.connect(str(db_file), check_same_thread=False)
        try:
            row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            chunk_count = row[0] if row else 0

            def _meta(key: str) -> str | None:
                r = conn.execute(
                    "SELECT value FROM meta WHERE key = ?", (key,)
                ).fetchone()
                return r[0] if r else None

            embedding_model = _meta("embedding_model")
            last_indexed = _meta("last_indexed")
        finally:
            conn.close()

        return StatResult(
            chunk_count=chunk_count,
            embedding_model=embedding_model,
            last_indexed=last_indexed,
        )
