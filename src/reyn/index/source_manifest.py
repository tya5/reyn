"""SourceManifest singleton + sources.yaml file SSoT (ADR-0033 Phase 1).

Registry of indexed sources. File SSoT under ``<workspace_root>/.reyn/index/sources.yaml``
with a per-process in-memory cache. Singleton per workspace via
``get_source_manifest(workspace_root)``.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import yaml

# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class SourceEntry:
    """A single source entry in the manifest."""

    name: str
    description: str
    path: str
    backend: str = "sqlite"
    last_indexed: str | None = None  # ISO 8601 UTC
    chunk_count: int = 0
    embedding_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the on-disk YAML structure (name is the key, not a field)."""
        d: dict[str, Any] = {
            "description": self.description,
            "path": self.path,
            "backend": self.backend,
            "chunk_count": self.chunk_count,
        }
        if self.last_indexed is not None:
            d["last_indexed"] = self.last_indexed
        if self.embedding_model is not None:
            d["embedding_model"] = self.embedding_model
        return d

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "SourceEntry":
        """Deserialise from on-disk YAML structure."""
        return cls(
            name=name,
            description=data.get("description", ""),
            path=data.get("path", ""),
            backend=data.get("backend", "sqlite"),
            last_indexed=data.get("last_indexed"),
            chunk_count=int(data.get("chunk_count", 0)),
            embedding_model=data.get("embedding_model"),
        )


# ── Errors ────────────────────────────────────────────────────────────────────


class SourceLockedError(Exception):
    """Raised when acquire_source_lock fails because source is in use."""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    """Check if a PID is alive without sending a signal that kills it."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# ── Main class ────────────────────────────────────────────────────────────────


class SourceManifest:
    """Registry of indexed sources. File SSoT + per-process mem cache.

    Singleton-per-workspace via ``get_source_manifest(workspace_root)``.
    Atomic file write on update; mem cache invalidated on update.

    Cross-process cache invalidation: ``get_all`` / ``get`` / ``format_for_prompt``
    check ``sources.yaml`` mtime before returning cached data.  If mtime advanced
    (another process wrote the file), the cache is reloaded transparently.

    NOTE — race window: the mtime comparison is best-effort.  A write that
    completes between our ``stat()`` call and the previous ``stat()`` is
    technically invisible until the next call.  For strict multi-process safety
    (e.g. indexer + agent writing simultaneously), use ``fcntl``-based file
    locking (phase 2).  ``acquire_source_lock`` provides write-write advisory
    locking; the mtime poll handles read-after-external-write.
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._path = workspace_root / ".reyn" / "index" / "sources.yaml"
        self._cache: dict[str, SourceEntry] | None = None
        self._loaded_mtime: float | None = None
        self._lock = asyncio.Lock()  # async safety for concurrent updates

    # ── Private ───────────────────────────────────────────────────────────────

    def _is_cache_stale(self) -> bool:
        """Return True if sources.yaml has changed since the cache was loaded.

        Best-effort mtime check — see class docstring for race window caveat.
        """
        try:
            current_mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            # File was deleted after we cached it → treat non-empty cache as stale.
            return self._cache is not None and bool(self._cache)
        return self._loaded_mtime is None or current_mtime > self._loaded_mtime

    async def _reload_from_file(self) -> None:
        """Read sources.yaml into cache and update _loaded_mtime.

        Caller MUST hold ``self._lock``.
        """
        if self._path.exists():
            raw: dict[str, Any] = yaml.safe_load(
                self._path.read_text(encoding="utf-8")
            ) or {}
            self._cache = {
                name: SourceEntry.from_dict(name, data)
                for name, data in raw.items()
            }
            self._loaded_mtime = self._path.stat().st_mtime
        else:
            self._cache = {}
            self._loaded_mtime = None

    async def _atomic_write(self) -> None:
        """Write current mem cache to disk atomically (write → fsync → rename).

        Caller MUST hold ``self._lock``.  Updates ``_loaded_mtime`` to the
        newly written file so the next ``get_all`` does not trigger an
        unnecessary reload.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".yaml.tmp")
        payload = {
            name: entry.to_dict()
            for name, entry in (self._cache or {}).items()
        }
        text = yaml.safe_dump(payload, sort_keys=True, allow_unicode=True)
        tmp.write_text(text, encoding="utf-8")
        # fsync for durability before rename
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        tmp.replace(self._path)  # atomic rename on POSIX
        # Record the mtime of the file we just wrote so subsequent reads
        # don't trigger a spurious reload caused by our own write.
        try:
            self._loaded_mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._loaded_mtime = None

    # ── Public async API ──────────────────────────────────────────────────────

    async def load(self) -> dict[str, SourceEntry]:
        """Load from file (or empty dict). Populates mem cache."""
        async with self._lock:
            await self._reload_from_file()
            return dict(self._cache)  # type: ignore[arg-type]

    async def get_all(self) -> dict[str, SourceEntry]:
        """Return all entries, reloading from file if the cache is stale.

        Staleness is detected via ``sources.yaml`` mtime so that writes from
        another process are picked up without an explicit ``load()`` call.
        """
        async with self._lock:
            if self._cache is None or self._is_cache_stale():
                await self._reload_from_file()
            return dict(self._cache)  # type: ignore[arg-type]

    async def get(self, name: str) -> SourceEntry | None:
        """Get a single entry by name."""
        entries = await self.get_all()
        return entries.get(name)

    async def upsert(self, entry: SourceEntry) -> None:
        """Add or update entry. Atomic file write + mem cache update."""
        async with self._lock:
            if self._cache is None:
                await self._reload_from_file()
            assert self._cache is not None
            self._cache[entry.name] = entry
            await self._atomic_write()

    async def remove(self, name: str) -> bool:
        """Remove entry. Returns True if removed, False if it didn't exist.

        Atomic file write + mem cache update.
        """
        async with self._lock:
            if self._cache is None:
                await self._reload_from_file()
            assert self._cache is not None
            if name not in self._cache:
                return False
            del self._cache[name]
            await self._atomic_write()
            return True

    async def format_for_prompt(self) -> str:
        """Render sources list for router system prompt injection.

        Empty case: returns a getting-started hint (UX gap fix A).

        Non-empty case: returns markdown with N sources listed.
        """
        entries = await self.get_all()

        if not entries:
            # JSON-form invocation (= flag form does not exist; the previous
            # text was hard-coded into the router system prompt and was
            # actively teaching the LLM the wrong syntax — fixed in the
            # 1.0 release-readiness wave).
            return (
                "## Indexed sources (0 available)\n"
                "\n"
                "No indexed sources yet. To enable retrieval over your data:\n"
                "\n"
                "```\n"
                "reyn run index_docs '{\"source\":\"<name>\",\"path\":\"<glob>\","
                "\"description\":\"<text>\"}'\n"
                "```\n"
                "\n"
                "Examples:\n"
                '- `reyn run index_docs \'{"source":"memory","path":".reyn/memory/*.md",'
                '"description":"User notes"}\'`\n'
                '- `reyn run index_docs \'{"source":"my_code","path":"src/**/*.py",'
                '"description":"Python source"}\'`\n'
            )

        n = len(entries)
        lines = [f"## Indexed sources ({n} available)", ""]
        for entry in entries.values():
            lines.append(
                f"- **{entry.name}** — {entry.description} ({entry.chunk_count} chunks)"
            )
        lines.append("")
        lines.append("Use the `recall` tool with `sources=[<name>, ...]` to search.")
        return "\n".join(lines)

    @asynccontextmanager
    async def acquire_source_lock(self, name: str) -> AsyncIterator[None]:
        """Async context manager for source-level advisory lock (UX gap fix D).

        Writes a marker file at ``.reyn/index/<name>/.lock`` with PID + timestamp.
        Raises ``SourceLockedError`` if the source is currently being indexed by
        a live process.  Stale locks (dead PID) are reaped automatically.
        """
        lock_path = self._workspace_root / ".reyn" / "index" / name / ".lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if an existing lock is held by a live process
        if lock_path.exists():
            try:
                lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
                holder_pid = int(lock_data.get("pid", 0))
                if holder_pid and _pid_alive(holder_pid):
                    raise SourceLockedError(
                        f"Source '{name}' is currently being indexed by PID"
                        f" {holder_pid}. Wait for completion or kill the holder."
                    )
                # Stale lock — fall through and take over
            except (json.JSONDecodeError, ValueError):
                pass  # Corrupted lock file; take over

        # Acquire the lock
        lock_path.write_text(
            json.dumps({"pid": os.getpid(), "ts": time.time()}),
            encoding="utf-8",
        )
        try:
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


# ── Module-level singleton registry (per-workspace) ───────────────────────────

_MANIFESTS: dict[Path, SourceManifest] = {}


def get_source_manifest(workspace_root: Path) -> SourceManifest:
    """Get or create the SourceManifest singleton for a workspace."""
    workspace_root = workspace_root.resolve()
    if workspace_root not in _MANIFESTS:
        _MANIFESTS[workspace_root] = SourceManifest(workspace_root)
    return _MANIFESTS[workspace_root]
