"""Config-driven Task backend selection (#1953 slice 3a).

A caller builds a Task backend via :func:`create_task_backend` — config-driven
(``in-memory`` for tests / ephemeral, ``sqlite`` for durable), never hardcoded. The
sqlite ``path`` is supplied by the caller.

#2180: the AGENT-keyed sqlite backend (``.reyn/agents/<name>/state/tasks.db`` — one db
file per AGENT, SHARED across that agent's sessions) is constructed at exactly ONE seam,
``AgentRegistry.task_backend_for``, which holds a SINGLE ``SqliteTaskBackend`` per agent
(one connection). Sessions get that shared instance — they never construct an agent-keyed
backend directly (a direct construction would open an N+1 connection and reintroduce the
#2125 cross-connection write race). The earlier per-session ``per_session_sqlite_backend``
helper that built N instances over the shared file was removed in #2180.
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
