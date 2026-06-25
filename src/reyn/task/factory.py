"""Config-driven Task backend selection (#1953 slice 3a).

The session factory calls :func:`create_task_backend` to build the per-session backend
INSTANCE it injects into ``Session(task_backend=...)`` (each session constructs its own
backend object — note the sqlite DB FILE it opens is agent-keyed/shared, see below). The
choice is config-driven (``in-memory`` for tests / ephemeral, ``sqlite`` for durable) —
never hardcoded.

The sqlite ``path`` is supplied by the caller. NOTE (#2128): the
``per_session_sqlite_backend`` path below is AGENT-keyed
(``.reyn/agents/<name>/state/tasks.db``) — one db file per AGENT, SHARED across that
agent's sessions, NOT one db per session. The earlier "one db per session" wording was
wrong; per-session isolation + the shared-file connection model are tracked in #2180.
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


def per_session_sqlite_backend(agent_name: str) -> "SqliteTaskBackend":
    """#1953 slice R, I-5=(A): the sqlite Task backend at the agent's default state
    dir — so in-session ``task.*`` ops are durable AND participate in the rewind
    window (alongside runtime-snapshot + workspace).

    #2128 correction: the path is AGENT-keyed (``.reyn/agents/<name>/state/tasks.db``),
    so it is ONE db file per AGENT, SHARED across that agent's sessions — NOT one db
    per session (the function name + the old "per-session" wording are misleading; the
    shared-file connection model + true per-session isolation are tracked in #2180).
    Each session still constructs its OWN ``SqliteTaskBackend`` instance over this
    shared path (N connections to one file). Used by the single-tenant local frontends
    (cli/chat, stdio-MCP); A2A/web pass ``None`` (the process-singleton A2A surface is
    read directly, not threaded into a session — its tasks stay durable but un-rewound,
    the cross-session fan-out tracked in #1997).
    """
    from pathlib import Path  # noqa: PLC0415

    path = Path(".reyn") / "agents" / agent_name / "state" / "tasks.db"
    return create_task_backend("sqlite", path=str(path))
