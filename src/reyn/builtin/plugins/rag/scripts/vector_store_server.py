"""Builtin vector-store MCP server -- wraps ``sqlite-vec`` (FP-0063 P2).

P1 (the OSS selection spike) found no off-the-shelf MCP vector-DB server
that accepts externally pre-computed vectors (FP-0057 C1 -- reyn is the
SOLE embedder; a server that embeds internally splits the vector space and
breaks C4's "one source = one embedding model" invariant). The owner then
approved writing this MCP surface ourselves, backed by ``sqlite-vec``
(dual MIT/Apache-2.0 licensed, actively maintained, a pure-C SQLite
extension with **no bundled model** -- it never downloads anything, so it
carries none of the HuggingFace-fetch hazard FP-0057 line 55 recorded).

**Portable extension loading -- a discovered, not assumed, dependency.**
``sqlite-vec`` loads via ``sqlite3.Connection.load_extension``, but the
stdlib ``sqlite3`` module is not guaranteed to be built with loadable-
extension support -- e.g. this very repository's own pyenv-built
CPython 3.12.7 ``.venv`` has ``hasattr(sqlite3.Connection,
"enable_load_extension") is False``. Rather than fail unpredictably
depending on the operator's Python distribution, this module uses
``apsw`` (Another Python SQLite Wrapper, zlib-like "any-OSI" license) --
it statically bundles its own SQLite build with extension loading always
enabled, so ``sqlite-vec`` loads deterministically regardless of how the
host interpreter's ``sqlite3`` module was compiled. This is reported as a
genuine environment finding in the PR body per the task's "do not paper
over it" instruction, not silently worked around.

Five gates satisfied by construction (proposal 0063 P2 spec):

1. **Pre-computed vectors** -- ``upsert`` takes a ``vectors`` array
   parallel to its ``items``; this module never computes an embedding
   itself.
2. **User-specified single sqlite file** -- every tool call takes an
   explicit ``db_path``; the schema is created lazily in that one file.
3. **top-k + metadata filter query** -- ``query`` runs a plain SQL
   ``WHERE`` over the metadata columns, joined against the KNN match.
4. **Metadata passthrough** -- the ``reyn_rag_chunks`` table carries the
   full ``ChunkMetadata`` shape (source_path/source_type/content_hash/
   embedding_model/chunk_index/size_tokens/parent_context/extra).
5. **Generic ops for a pipeline-owned diff** -- ``list_metadata`` (no
   vectors, Chroma ``get(where=...)`` shape), ``upsert`` (replaces by
   (source_path, chunk_index), no duplication), ``delete``. The
   ``content_hash`` add/update/remove diff logic is deliberately NOT here
   -- it lives in the ingest pipeline (P3), per C5 ownership settled at
   co-vet: keeping the server generic keeps a future backend swap
   "re-point the MCP server", not "find a server that implements our diff
   semantics". ``upsert`` deriving its own primary key (#2972) does not
   breach that line: a key FORMULA is not diff semantics -- the pipeline
   still decides WHICH chunks are new/changed/removed, and reads opaque
   ``id`` values back out of ``list_metadata`` to drive ``delete``.

``SqliteVecStore`` holds the storage logic as a plain Python class so it
can be exercised directly (no MCP transport needed) by tests; the module-
level ``mcp = FastMCP(...)`` object below is the thin tool-call skin over
it, run directly as ``python <this file's path>`` (stdio transport) --
either from a dev checkout, or (ADR 0064 P5/§3.11b, #3209) via the
operator/LLM's OWN per-plugin venv interpreter (created per the installing
skill's SETUP steps -- never one ``plugin_install`` provisions) once
``plugin_management__install(source={"kind": "builtin", "name": "rag"})`` has
registered the server.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ChunkMetadata's field order (FP-0057 / ADR-0033, reyn.schemas.models).
# Duplicated here rather than imported: this module is builtin CONTENT
# (proposal 0063's owner framing -- "MCP means bundling standard MCP
# servers", not reyn-core code), so it deliberately has no reyn-core
# import dependency and can be copied wholesale by a user who wants a
# different backend (owner: "user who wants a different vector DB copies
# the builtin and re-points the MCP server" -- the extension mechanism).
METADATA_COLUMNS: tuple[str, ...] = (
    "source_path",
    "source_type",
    "content_hash",
    "embedding_model",
    "chunk_index",
    "size_tokens",
    "parent_context",
)


class VectorDimensionMismatchError(ValueError):
    """Raised when an upserted vector's length doesn't match the store's
    established dimension (fixed at the first upsert into a fresh db)."""


def _describe_open_failure(db_path: str) -> str | None:
    """Reproduce a failed open of *db_path* through plain ``open()`` and return
    the OS's own error text, or None if it cannot be reproduced (#3009).

    Exists because ``apsw.Connection`` DISCARDS errno. MEASURED, same path, same
    Seatbelt profile, same denied directory::

        apsw.Connection(p)  -> CantOpenError("unable to open database file")
        open(p, "a")        -> PermissionError("[Errno 1] Operation not
                                                permitted: '<path>'")

    Only the second carries a signature anything downstream can classify: reyn's
    MCP client keys its "add the path to `write_paths`" hint off that errno text
    (``reyn/mcp/client.py::_looks_like_write_denial``), and "unable to open
    database file" is indistinguishable, at that layer, from a typo. So on the
    failure path only, we ask the OS the same question in a way that answers it.

    Returning None (the open SUCCEEDED, so apsw failed for some unrelated reason
    — a corrupt file, a lock) is a first-class outcome: the caller then re-raises
    apsw's own error untouched rather than attaching a diagnosis that would be a
    guess.
    """
    path = Path(db_path)
    existed = path.exists()
    try:
        with open(path, "a"):
            pass
    except OSError as exc:
        return str(exc)
    # The probe could open it, so it must not leave a file behind: apsw already
    # refused this path, and a stray 0-byte file is a VALID empty sqlite db that
    # would silently become the store on the operator's next attempt.
    if not existed:
        try:
            path.unlink()
        except OSError:  # noqa: S110 -- best-effort cleanup; the real error wins
            pass
    return None


def _connect(db_path: str) -> Any:
    """``apsw.Connection(db_path)``, re-raising a failure with the OS-level
    reason restored (see :func:`_describe_open_failure` for why it is missing).
    """
    import apsw  # noqa: PLC0415 -- optional extra (builtin-rag)

    try:
        return apsw.Connection(db_path)
    except apsw.Error as exc:
        detail = _describe_open_failure(db_path)
        if detail is None:
            raise
        raise OSError(f"cannot open sqlite db at {db_path!r}: {detail}") from exc


class SqliteVecStore:
    """Thin generic vector store over one sqlite file, backed by
    ``sqlite-vec`` loaded through ``apsw`` (see module docstring for why
    apsw, not stdlib ``sqlite3``).

    One instance == one open connection to one ``db_path``. Callers
    (the MCP tool functions below, or a test) construct one per call
    and close it -- these stores are opened by a user-named file path
    that can be anywhere, so no connection pooling / caching is done
    (this is "builtin content", not a service; simplicity over
    performance is the deliberate trade-off, matching R2 readability).
    """

    def __init__(self, db_path: str) -> None:
        import sqlite_vec  # noqa: PLC0415 -- optional extra (builtin-rag)

        self._db_path = db_path
        # LOAD-BEARING FOR DIAGNOSABILITY, not just convenience (#3009). The
        # obvious cleanup -- "drop this, sqlite creates the file itself" -- is
        # WRONG: this mkdir is the first thing a sandbox write denial hits, and
        # it raises PermissionError with the errno and the offending path in its
        # message, which is what lets reyn's MCP client tell the operator "the
        # sandbox denied this; add the path to `write_paths`" instead of showing
        # a bare sqlite error. `apsw` throws that signature away (see _connect).
        # Deleting this line would not break a single test -- it would only make
        # every denial in the common case undiagnosable.
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # ...and _connect covers the case this mkdir CANNOT: an operator who
        # created the target directory themselves leaves mkdir a no-op with
        # nothing to raise, so the denial first surfaces at the open below.
        # MEASURED: that case reported "unable to open database file" -- marker-
        # free -- until _connect restored the errno. Neither site is redundant.
        self._conn = _connect(db_path)
        self._conn.enable_load_extension(True)
        self._conn.load_extension(sqlite_vec.loadable_path())
        self._conn.enable_load_extension(False)
        self._ensure_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteVecStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- schema -------------------------------------------------------

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reyn_rag_chunks (
                rag_id TEXT UNIQUE NOT NULL,
                source_path TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'generic',
                content_hash TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                size_tokens INTEGER NOT NULL DEFAULT 0,
                parent_context TEXT,
                extra TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS reyn_rag_config (dim INTEGER NOT NULL)"
        )

    def _dim(self) -> int | None:
        row = self._conn.execute("SELECT dim FROM reyn_rag_config").fetchone()
        return int(row[0]) if row else None

    def _ensure_vec_table(self, dim: int) -> None:
        existing = self._dim()
        if existing is None:
            self._conn.execute(
                "INSERT INTO reyn_rag_config(dim) VALUES (?)", (dim,)
            )
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS reyn_rag_vectors "
                f"USING vec0(embedding float[{dim}])"
            )
        elif existing != dim:
            raise VectorDimensionMismatchError(
                f"store {self._db_path!r} was initialized with dim={existing}; "
                f"got a vector of dim={dim}. One sqlite file holds one "
                "embedding-vector-space (FP-0057 C4: different model -> "
                "different source/store)."
            )

    # -- ops ------------------------------------------------------------

    @staticmethod
    def _rag_id(item: dict[str, Any]) -> str:
        """This store's primary key for a chunk: ``<source_path>::<chunk_index>``.

        DERIVED here rather than supplied by the caller (#2972). The store
        owns its own keyspace -- and the caller cannot build this key anyway:
        the ingest pipeline's R1 expression language has no number->string
        coercion (``'a.txt' + '::' + 3`` raises "arithmetic '+' requires two
        numbers"), which is one of the reasons the pipeline used to shell out
        to ``python3``. Callers identify a chunk by (source_path,
        chunk_index) and read opaque ``id`` values back out of
        ``list_metadata`` for ``delete``, so no caller ever spells this
        format itself.

        This is a key FORMULA, not diff semantics: the content_hash
        add/update/remove logic (C5) deliberately stays in the ingest
        pipeline (see module docstring gate 5).
        """
        return f"{item['source_path']}::{int(item.get('chunk_index', 0))}"

    def upsert(
        self,
        items: list[dict[str, Any]],
        vectors: list[list[float]],
        embedding_model: str,
        parent_context: str | None = None,
    ) -> int:
        """Insert or replace each item, pairing it with ``vectors[i]`` BY
        POSITION. Returns the count upserted. Replaces rather than
        duplicates: an existing key is deleted (both tables) before the new
        row lands.

        ``items`` and ``vectors`` are PARALLEL ARRAYS (#2972): each item is
        ``{"source_path", "content_hash", "chunk_index", "size_tokens"}`` and
        its vector is the element at the same index of ``vectors`` -- the
        shape reyn's ``embed`` tool already returns. Pairing by index is done
        HERE because the caller (the ingest pipeline) cannot: R1 has no
        index-based zip. That gap is exactly what used to force the ingest
        pipeline to shell out to a bundled python helper, binding reyn to
        the ambient ``PATH``'s interpreter (#2972).

        ``embedding_model`` is stamped onto every row: it must be the model
        that ACTUALLY produced these vectors (the resolved id, never a
        model-CLASS alias), so the column can never disagree with the vectors
        beside it (FP-0057 C4). ``parent_context`` tags every row with the
        ingest root, scoping a later "what did THIS folder ingest" query.
        """
        import sqlite_vec  # noqa: PLC0415

        if len(items) != len(vectors):
            raise ValueError(
                f"upsert: {len(items)} items but {len(vectors)} vectors -- "
                "each item is paired with the vector at its own index, so "
                "the embedder's output order must match the items it was "
                "called with"
            )
        count = 0
        for item, vector in zip(items, vectors, strict=True):
            rag_id = self._rag_id(item)
            metadata = {
                **item,
                "embedding_model": embedding_model,
                "parent_context": parent_context,
            }
            self._ensure_vec_table(len(vector))
            self._delete_one(rag_id)
            extra = metadata.get("extra") or {}
            self._conn.execute(
                """
                INSERT INTO reyn_rag_chunks
                    (rag_id, source_path, source_type, content_hash,
                     embedding_model, chunk_index, size_tokens,
                     parent_context, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rag_id,
                    metadata.get("source_path", ""),
                    metadata.get("source_type", "generic"),
                    metadata.get("content_hash", ""),
                    metadata.get("embedding_model", ""),
                    int(metadata.get("chunk_index", 0)),
                    int(metadata.get("size_tokens", 0)),
                    metadata.get("parent_context"),
                    json.dumps(extra),
                ),
            )
            new_rowid = self._conn.last_insert_rowid()
            self._conn.execute(
                "INSERT INTO reyn_rag_vectors(rowid, embedding) VALUES (?, ?)",
                (new_rowid, sqlite_vec.serialize_float32(list(vector))),
            )
            count += 1
        return count

    def _delete_one(self, rag_id: str) -> None:
        row = self._conn.execute(
            "SELECT rowid FROM reyn_rag_chunks WHERE rag_id = ?", (rag_id,)
        ).fetchone()
        if row is None:
            return
        rowid = row[0]
        self._conn.execute("DELETE FROM reyn_rag_chunks WHERE rowid = ?", (rowid,))
        # The vec0 virtual table may not exist yet if this id was never
        # actually vectorized (defensive; upsert always creates it first).
        if self._dim() is not None:
            self._conn.execute(
                "DELETE FROM reyn_rag_vectors WHERE rowid = ?", (rowid,)
            )

    def delete(self, ids: list[str]) -> int:
        """Delete each id from both tables. Returns the count actually
        deleted (ids that don't exist are silently skipped)."""
        deleted = 0
        for rag_id in ids:
            row = self._conn.execute(
                "SELECT rowid FROM reyn_rag_chunks WHERE rag_id = ?", (rag_id,)
            ).fetchone()
            if row is None:
                continue
            self._delete_one(rag_id)
            deleted += 1
        return deleted

    @staticmethod
    def _row_to_metadata(row: tuple, columns: tuple[str, ...]) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        for col, val in zip(columns, row, strict=True):
            if col == "extra":
                meta["extra"] = json.loads(val) if val else {}
            else:
                meta[col] = val
        return meta

    def _build_filter_clause(
        self, filters: dict[str, Any] | None, *, alias: str = "",
    ) -> tuple[str, list[Any]]:
        if not filters:
            return "", []
        prefix = f"{alias}." if alias else ""
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in filters.items():
            if key not in METADATA_COLUMNS:
                raise ValueError(
                    f"unsupported filter key {key!r}; must be one of "
                    f"{METADATA_COLUMNS} (plain-SQL-WHERE metadata filtering "
                    "only -- 'extra' is a JSON blob and not filterable here)"
                )
            clauses.append(f"{prefix}{key} = ?")
            params.append(value)
        return " AND " + " AND ".join(clauses), params

    def list_metadata(
        self, filters: dict[str, Any] | None = None, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Metadata-filtered listing WITHOUT vectors (Chroma
        ``get(where=...)`` shape) -- the generic op the pipeline's
        content_hash diff (C5) reads from."""
        where, params = self._build_filter_clause(filters)
        columns = ("rag_id", *METADATA_COLUMNS, "extra")
        sql = (
            f"SELECT {', '.join(columns)} FROM reyn_rag_chunks WHERE 1=1{where} "
            f"LIMIT ?"
        )
        rows = self._conn.execute(sql, (*params, limit)).fetchall()
        out = []
        for row in rows:
            rag_id = row[0]
            meta = self._row_to_metadata(row[1:], (*METADATA_COLUMNS, "extra"))
            out.append({"id": rag_id, "metadata": meta})
        return out

    def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """top-k nearest-neighbor query with an optional plain-SQL metadata
        WHERE filter. Returns ``[{"id", "distance", "metadata"}, ...]``
        ordered nearest-first.

        The KNN ``k`` bound is set to the store's total row count (not
        ``top_k``) so the metadata filter is applied to the FULL
        candidate set before truncating to ``top_k`` -- otherwise a
        vec0 ``k=top_k`` clause could exclude a matching row before the
        ``WHERE`` filter ever sees it. This module targets a personal/
        folder-scale RAG store, where a full KNN scan is cheap; a future
        backend swap free to reintroduce a real ANN index.
        """
        import sqlite_vec  # noqa: PLC0415

        dim = self._dim()
        if dim is None:
            return []
        if len(vector) != dim:
            raise VectorDimensionMismatchError(
                f"query vector has dim={len(vector)}; store dim={dim}"
            )
        total = self._conn.execute(
            "SELECT COUNT(*) FROM reyn_rag_vectors"
        ).fetchone()[0]
        if total == 0:
            return []
        where, params = self._build_filter_clause(filters, alias="c")
        columns = ("c.rag_id", *(f"c.{c}" for c in METADATA_COLUMNS), "c.extra")
        sql = (
            f"SELECT {', '.join(columns)}, v.distance "
            "FROM reyn_rag_vectors v "
            "JOIN reyn_rag_chunks c ON c.rowid = v.rowid "
            f"WHERE v.embedding MATCH ? AND k = ?{where} "
            "ORDER BY v.distance LIMIT ?"
        )
        rows = self._conn.execute(
            sql,
            (sqlite_vec.serialize_float32(list(vector)), total, *params, top_k),
        ).fetchall()
        out = []
        for row in rows:
            rag_id = row[0]
            meta = self._row_to_metadata(row[1:-1], (*METADATA_COLUMNS, "extra"))
            distance = row[-1]
            out.append({"id": rag_id, "distance": distance, "metadata": meta})
        return out


# ---------------------------------------------------------------------------
# MCP tool skin (fastmcp). Imports are deferred to server construction so
# this module remains importable (for the ``SqliteVecStore`` class above)
# in environments without fastmcp/apsw/sqlite-vec/chonkie installed.
# ---------------------------------------------------------------------------


def build_server() -> Any:
    """Build the ``FastMCP`` server exposing ``SqliteVecStore`` as tools."""
    from fastmcp import FastMCP  # noqa: PLC0415

    mcp = FastMCP("reyn-builtin-vector-store")

    @mcp.tool
    def upsert(
        db_path: str,
        items: list[dict[str, Any]],
        vectors: list[list[float]],
        embedding_model: str,
        parent_context: str | None = None,
    ) -> dict[str, Any]:
        """Insert or replace vector+metadata items in the sqlite-vec store
        at db_path. `items` and `vectors` are PARALLEL ARRAYS: item[i] is
        stored with vector[i]. Each item: {"source_path": str,
        "content_hash": str, "chunk_index": int, "size_tokens": int}.
        `embedding_model` must name the model that actually produced these
        vectors (stamped on every row); `parent_context` tags each row with
        the ingest root. A chunk is keyed by (source_path, chunk_index) and
        an existing one is replaced (never duplicated). Raises if
        len(items) != len(vectors)."""
        with SqliteVecStore(db_path) as store:
            n = store.upsert(
                items, vectors, embedding_model, parent_context=parent_context,
            )
        return {"upserted": n}

    @mcp.tool
    def query(
        db_path: str,
        vector: list[float],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """top-k nearest-neighbor query against db_path, with an optional
        plain-SQL equality filter over ChunkMetadata columns (source_path,
        source_type, content_hash, embedding_model, chunk_index,
        size_tokens, parent_context). Returns nearest-first
        [{"id", "distance", "metadata"}, ...]."""
        with SqliteVecStore(db_path) as store:
            return store.query(vector, top_k=top_k, filters=filters)

    @mcp.tool
    def list_metadata(
        db_path: str,
        filters: dict[str, Any] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Metadata-filtered listing WITHOUT vectors (Chroma
        get(where=...) shape) -- for a pipeline-owned content_hash diff.
        Returns [{"id", "metadata"}, ...]."""
        with SqliteVecStore(db_path) as store:
            return store.list_metadata(filters=filters, limit=limit)

    @mcp.tool
    def delete(db_path: str, ids: list[str]) -> dict[str, Any]:
        """Delete the given ids from db_path (both metadata and vector
        tables). Returns {"deleted": count}; unknown ids are skipped."""
        with SqliteVecStore(db_path) as store:
            n = store.delete(ids)
        return {"deleted": n}

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
