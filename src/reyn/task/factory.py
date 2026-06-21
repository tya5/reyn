"""Config-driven Task backend selection (#1953 slice 3a).

The session factory calls :func:`create_task_backend` to build the session-scoped
backend it injects into ``Session(task_backend=...)``. The choice is config-driven
(``in-memory`` for tests / ephemeral, ``sqlite`` for durable) — never hardcoded.

The sqlite ``path`` is session-scoped (one db per session, so the task store
participates in that session's rewind window); the exact path is supplied by the
caller that owns the session's state dir (finalized with §24). Path-agnostic here.
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
            raise ValueError("sqlite task backend requires a 'path' (session-scoped db)")
        return SqliteTaskBackend(path)
    raise ValueError(f"unknown task backend kind: {kind!r} (expected 'sqlite' or 'in-memory')")


def per_session_sqlite_backend(agent_name: str) -> "SqliteTaskBackend":
    """#1953 slice R, I-5=(A): a per-session sqlite Task backend at the agent's
    default state dir — so in-session ``task.*`` ops are durable AND participate
    in that session's rewind window (alongside runtime-snapshot + workspace).

    The path is the sibling of the Session's default ``_snapshot_path`` /
    ``generations`` dir (``.reyn/agents/<name>/state/tasks.db``), centralized here
    so the capture (``cut_generation``) and restore (``_restore_task_active``)
    operate on the same db. Used by the single-tenant local frontends (cli/chat,
    stdio-MCP); A2A/web pass ``None`` (the process-singleton A2A surface is read
    directly, not threaded into a session — its tasks stay durable but un-rewound,
    the cross-session fan-out tracked in #1997).
    """
    from pathlib import Path  # noqa: PLC0415

    path = Path(".reyn") / "agents" / agent_name / "state" / "tasks.db"
    return create_task_backend("sqlite", path=str(path))
