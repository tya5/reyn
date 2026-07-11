"""Tier 2: MCP-over-SSE wiring on the FastAPI gateway.

Pins the contract that ``/mcp/sse`` and ``/mcp/messages`` are mounted
on the same app the browser UI uses, and that the POST endpoint
returns sane 4xx responses on bad / missing session_id (= the SDK's
own validation surface — we test that we exposed it intact, not that
the SDK is correct).

We do NOT exercise the full JSON-RPC handshake here — that's owned
by the upstream ``mcp`` SDK, the unit tests in
``tests/test_mcp_server.py`` already cover the backing impl
(``list_agents_impl`` / ``send_to_agent_impl``) which both transports
share.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the worktree src is importable (mirrors test_smoke.py).
_WORKTREE_SRC = Path(__file__).parent.parent.parent / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

# Skip the whole module if optional deps are missing.
pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")
pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")
pytest.importorskip("mcp", reason="mcp not installed ([mcp] extra missing)")


# ---------------------------------------------------------------------------
# 2. POST /mcp/messages without session_id → 4xx
# ---------------------------------------------------------------------------
#
# (A former "routes are mounted" test lived here, pinning literal
# ``app.routes`` path strings for ``/mcp/sse`` and ``/mcp/messages``.
# FastAPI 0.139 changed ``include_router``'s internal representation
# (routes now surface as a lazy ``_IncludedRouter`` wrapper, not a
# flattened ``Route`` with a ``.path``), so the pin failed even though
# both routes remain reachable — the exact "NEVER pin internal
# structure" trap the testing policy warns about. Deleted as a
# redundant duplicate: ``/mcp/messages`` reachability is already proven
# behaviorally by the two tests below (a 4xx response means the SDK's
# own mount handled the request — a 404 would mean unmounted) and by
# ``tests/web/test_surface_registry.py``'s disabled/enabled surface
# checks. ``/mcp/sse`` is appended to the app's route table in the
# very same ``_mount_mcp`` call (``surfaces.py``) that appends
# ``/mcp/messages``, guarded by the same enabled-check, so proving one
# reachable is strong evidence for the other; opening a real
# ``GET /mcp/sse`` stream from a sync ``TestClient`` was tried and
# hangs (verified empirically) because the SSE handshake needs a
# concurrent client, not just an app.routes membership check.)


def test_mcp_messages_missing_session_id_returns_4xx() -> None:
    """Tier 2: POST /mcp/messages with no session_id returns a 4xx
    error, not a 5xx.

    The SDK validates the query string itself; we just verify the
    mount delegates correctly. A 5xx here would mean we wrapped the
    ASGI app in something that swallows its own response (= regression
    we want to catch).
    """
    from reyn.interfaces.web.server import app
    from tests._support.web_auth import local_operator_client

    client = local_operator_client(app, raise_server_exceptions=False)
    response = client.post("/mcp/messages", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert 400 <= response.status_code < 500, (
        f"Expected 4xx for missing session_id, got {response.status_code}: {response.text}"
    )


# ---------------------------------------------------------------------------
# 3. POST /mcp/messages with malformed session_id → 4xx
# ---------------------------------------------------------------------------


def test_mcp_messages_invalid_session_id_returns_4xx() -> None:
    """Tier 2: POST /mcp/messages with a non-UUID session_id returns
    4xx, not 5xx. Pins the same delegation contract as the previous
    test on a different bad-input path.
    """
    from reyn.interfaces.web.server import app
    from tests._support.web_auth import local_operator_client

    client = local_operator_client(app, raise_server_exceptions=False)
    response = client.post(
        "/mcp/messages?session_id=not-a-uuid",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert 400 <= response.status_code < 500, (
        f"Expected 4xx for invalid session_id, got {response.status_code}: {response.text}"
    )
