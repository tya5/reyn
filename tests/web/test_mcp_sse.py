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
# 1. Routes are mounted
# ---------------------------------------------------------------------------


def test_mcp_routes_mounted() -> None:
    """Tier 2: the /mcp/sse GET route and the /mcp/messages mount are
    both present on the FastAPI app.

    Pins the wiring without exercising the SSE handshake: a missing
    Mount is the most likely regression if the router file is renamed
    or its include is reordered.
    """
    from reyn.web.server import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/mcp/sse" in paths, f"/mcp/sse missing from {paths}"
    assert "/mcp/messages" in paths, f"/mcp/messages missing from {paths}"


# ---------------------------------------------------------------------------
# 2. POST /mcp/messages without session_id → 4xx
# ---------------------------------------------------------------------------


def test_mcp_messages_missing_session_id_returns_4xx() -> None:
    """Tier 2: POST /mcp/messages with no session_id returns a 4xx
    error, not a 5xx.

    The SDK validates the query string itself; we just verify the
    mount delegates correctly. A 5xx here would mean we wrapped the
    ASGI app in something that swallows its own response (= regression
    we want to catch).
    """
    from fastapi.testclient import TestClient

    from reyn.web.server import app

    client = TestClient(app, raise_server_exceptions=False)
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
    from fastapi.testclient import TestClient

    from reyn.web.server import app

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/mcp/messages?session_id=not-a-uuid",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert 400 <= response.status_code < 500, (
        f"Expected 4xx for invalid session_id, got {response.status_code}: {response.text}"
    )
