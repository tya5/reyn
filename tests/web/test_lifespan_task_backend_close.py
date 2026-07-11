"""Tier 2: the web lifespan closes the sqlite task backend on shutdown.

Regression for the FP-0057-exposed leak: ``_lifespan`` (server.py) opened the
sqlite Task backend at ``.reyn/state/tasks.db`` on startup but never closed it
on shutdown. Because the module-level ``app`` singleton overwrites
``app.state.task_backend`` on the next startup, the orphaned sqlite connection
(and its WAL lock) leaked. The backend opens with ``PRAGMA busy_timeout=0``
(deliberate fail-fast), so an un-GC'd orphan overlapping the next open could
surface as ``sqlite3.OperationalError: database is locked`` — an order-dependent
test flake (it tripped on 3.11 once the FP-0057 test files shifted collection
order) and a real fd/lock leak across gateway restarts.

The contract pinned here is the fix itself, observed through the PUBLIC surface:
after the lifespan exits, the task backend it opened is CLOSED — calling a
public method (``get``) on it raises ``sqlite3.ProgrammingError`` (operating on
a closed database). This is deterministic (no reliance on GC timing or a lock
race): pre-fix the backend stays open and ``get`` returns ``None`` (no raise),
post-fix it is closed and ``get`` raises. ``busy_timeout=0`` is left untouched
(the fail-fast is intended design — the fix closes the leaking connection, it
does not make anything wait).

Real instances only: the real production FastAPI app driven by a real
Starlette ``TestClient``; a real ``SqliteTaskBackend``. No mocks. The backend
reference is captured inside the ``with`` block and held across shutdown so the
assertion targets the exact instance the lifespan owned.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

# Ensure the worktree src is importable even when the main-tree src appears
# earlier on sys.path (editable-install collision) — mirrors the sibling
# web tests so the fully-mounted worktree app is the one under test.
_WORKTREE_SRC = Path(__file__).parent.parent.parent / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

import pytest

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")
httpx = pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")

from fastapi.testclient import TestClient  # noqa: E402


def test_lifespan_closes_task_backend_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: after the web lifespan exits, its sqlite task backend is closed.

    Proves ``_lifespan`` closes ``app.state.task_backend`` on shutdown — a
    ``get`` on the held backend reference raises ``sqlite3.ProgrammingError``
    (closed database). Without the close the backend stays open and ``get``
    returns ``None``. The lifespan resolves ``.reyn/state/tasks.db`` relative
    to cwd, so ``chdir(tmp_path)`` isolates this run from the repo.
    """
    monkeypatch.chdir(tmp_path)
    from reyn.interfaces.web.server import app

    with TestClient(app, raise_server_exceptions=False) as client:
        # Force the lifespan to have fully started (task backend opened), then
        # capture the exact backend instance the lifespan owns so the
        # post-shutdown assertion targets it (and it is not GC-collected).
        client.get("/health")
        backend = client.app.state.task_backend

    assert backend is not None, "the lifespan should have opened a task backend"

    # Post-shutdown: the lifespan must have closed the connection. A public
    # read on a closed sqlite connection raises ProgrammingError; if the
    # backend were leaked (still open), get() would return None instead.
    with pytest.raises(sqlite3.ProgrammingError):
        asyncio.run(backend.get("nonexistent-task-id"))
