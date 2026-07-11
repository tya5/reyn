"""Tier 2: the P1 mount-front auth gate enforces auth on the non-AG-UI surfaces.

Security proof for ``reyn.interfaces.web.auth_gate.AuthGateMiddleware``. Before
P1 the REST control plane (``/api``), the A2A spine (``/a2a``), the MCP surface
(``/mcp``), and the resource-fetch routes had NO authentication check — on a
non-loopback bind they were reachable unauthenticated. The gate closes that.

Real instances only: the real production FastAPI app (with its real lifespan
building a real :class:`AuthContext`), driven by a real Starlette ``TestClient``
whose default client host (``testclient``) classifies as the cross-machine
network tier — so a request with no token is exactly the exposed case.

Strip-gate falsification (the load-bearing security assertion): with the gate
present, ``PATCH /api/budget/caps`` with no token is 401; remove the
``AuthGateMiddleware`` install from ``server.py`` and the same request reaches
the router and returns 200 — the no-token assertions go RED. That is the proof
the middleware, and only the middleware, is what closes the unbounded-spend hole.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure the worktree src is importable even when the main-tree src appears
# earlier on sys.path (editable-install collision) — mirrors the sibling
# route-mount tests so the fully-mounted worktree app is the one under test.
_WORKTREE_SRC = Path(__file__).parent.parent.parent / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

import pytest

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")
httpx = pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")

from fastapi.testclient import TestClient  # noqa: E402

from tests._support.web_auth import local_operator_asgi  # noqa: E402


def _reset_singletons() -> None:
    import reyn.interfaces.web.deps as deps

    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None


@pytest.fixture()
def tmp_project(tmp_path: Path) -> Path:
    """A minimal project root so the real budget/config/registry deps resolve."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    agent_dir = tmp_path / ".reyn" / "agents" / "default"
    agent_dir.mkdir(parents=True)
    (agent_dir / "profile.yaml").write_text(
        "name: default\nrole: ''\ncreated_at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def gated(tmp_project: Path, monkeypatch: pytest.MonkeyPatch):
    """Yield ``(client, token)`` for the real app on the network tier.

    The ``with`` fires the server lifespan, which builds the real AuthContext
    (generating a token); the network-tier client presents no token by default.
    """
    _reset_singletons()
    monkeypatch.setattr("reyn.config._find_project_root", lambda _: tmp_project)
    from reyn.interfaces.web.server import app

    app.dependency_overrides.clear()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, client.app.state.auth.token
    app.dependency_overrides.clear()
    _reset_singletons()


# ── The load-bearing strip-gate: unbounded-spend surface ─────────────────────


def test_patch_budget_caps_requires_token(gated) -> None:
    """Tier 2: PATCH /api/budget/caps no-token → 401; with token → 200.

    The sharpest gate (raise the cap → disable budget bounding). Stripping the
    middleware flips the no-token case to 200 (the router mutates the cap), which
    is the RED that proves this test pins the enforcement.
    """
    client, token = gated
    assert client.patch("/api/budget/caps", json={}).status_code == 401
    assert client.patch(f"/api/budget/caps?token={token}", json={}).status_code == 200


# ── The rest of the previously-ungated control plane + surfaces ──────────────


def test_permissions_delete_requires_token(gated) -> None:
    """Tier 2: DELETE /api/permissions/{key} with no token is refused (401)."""
    client, _ = gated
    assert client.delete("/api/permissions/some.key").status_code == 401


def test_control_plane_mutations_require_token(gated) -> None:
    """Tier 2: POST /api/agents and /api/topologies with no token are 401."""
    client, _ = gated
    assert client.post("/api/agents", json={}).status_code == 401
    assert client.post("/api/topologies", json={}).status_code == 401


def test_a2a_mcp_and_resources_require_token(gated) -> None:
    """Tier 2: the A2A, MCP, and resource-fetch surfaces refuse a no-token
    request (401) before the router runs."""
    client, _ = gated
    assert client.post("/a2a/agents/default", json={}).status_code == 401
    assert client.post("/mcp/messages", json={}).status_code == 401
    assert client.get("/agents/default/tool-results/x.txt").status_code == 401


def test_token_admits_gated_surfaces(gated) -> None:
    """Tier 2: with the configured token the gate does NOT block — each gated
    surface passes auth (past 401 into the handler)."""
    client, token = gated
    assert client.delete(f"/api/permissions/some.key?token={token}").status_code != 401
    assert client.post(f"/a2a/agents/default?token={token}", json={}).status_code != 401
    assert client.get(f"/agents/default/tool-results/x.txt?token={token}").status_code != 401


# ── Non-regression: preflight, open surfaces, AG-UI, UDS ─────────────────────


def test_cors_preflight_options_needs_no_token(gated) -> None:
    """Tier 2: a CORS preflight OPTIONS is answered 200 with no token (CORS
    stays outermost; OPTIONS is never gated)."""
    client, _ = gated
    resp = client.options(
        "/api/budget/caps",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "PATCH",
        },
    )
    assert resp.status_code == 200


def test_open_surfaces_need_no_token(gated) -> None:
    """Tier 2: the OpenUI shell root and /health stay open (no token)."""
    client, _ = gated
    assert client.get("/health").status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 302


def test_agui_is_skipped_and_stays_self_gated(gated) -> None:
    """Tier 2: /agui is skipped by the mount-front gate but its own per-handler
    gate still refuses a no-token request (byte-identical to before P1 — the gate
    does not double-handle nor weaken AG-UI)."""
    client, _ = gated
    resp = client.post("/agui/chat/default", json={"type": "user_message", "text": "hi"})
    assert resp.status_code == 401


def test_uds_local_operator_admitted_without_token(gated) -> None:
    """Tier 2: the same-machine UDS operator reaches the gated control plane with
    NO token — the P0 loopback/UDS relaxation is preserved (local operator not
    blocked by P1)."""
    client, _ = gated
    uds = TestClient(local_operator_asgi(client.app), raise_server_exceptions=False)
    assert uds.patch("/api/budget/caps", json={}).status_code == 200
