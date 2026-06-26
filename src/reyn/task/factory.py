"""Config-driven Task backend selection (#1953 slice 3a).

A caller builds a Task backend via :func:`create_task_backend` — config-driven
(``in-memory`` for tests / ephemeral, ``sqlite`` for durable), never hardcoded. The
sqlite ``path`` is supplied by the caller.

#2187 S1: the sqlite Task backend is GLOBAL (``.reyn/state/tasks.db`` — ONE db file per
process, task ⊥ agent/session), constructed at exactly ONE seam, the
``AgentRegistry.task_backend`` property, which holds a SINGLE ``SqliteTaskBackend`` (one
connection). Every session gets that one instance — they never construct a backend
directly (a direct construction would open an N+1 connection and reintroduce the #2125
cross-connection write race). This reverts the #2180 per-AGENT / #2186 per-SESSION splits
(a task is first-class, referenced by an owner, not living in a session — #2187 §2).
"""
from __future__ import annotations

from reyn.task.backend import InMemoryTaskBackend
from reyn.task.sqlite_backend import SqliteTaskBackend

_DEFAULT_KIND = "sqlite"


def create_task_backend(kind: str | None = None, *, path: str | None = None):
    """Build a Task backend by ``kind``.

    - ``"sqlite"`` (default) → :class:`SqliteTaskBackend` at ``path`` (required).
    - ``"in-memory"`` → :class:`InMemoryTaskBackend` (ephemeral; tests).

    Unknown kinds raise ``ValueError`` (fail-loud — no silent fallback that would
    mask a misconfigured backend).
    """
    chosen = (kind or _DEFAULT_KIND).strip().lower()
    if chosen in ("in-memory", "in_memory", "memory"):
        return InMemoryTaskBackend()
    if chosen == "sqlite":
        if not path:
            raise ValueError(
                "sqlite task backend requires a 'path' "
                "(#2128: agent-keyed db, shared across the agent's sessions)"
            )
        return SqliteTaskBackend(path)
    raise ValueError(f"unknown task backend kind: {kind!r} (expected 'sqlite' or 'in-memory')")
