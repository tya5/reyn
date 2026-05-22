"""Tier 2: resources router — HTTP fetch surface for path_ref bodies
(#385 β core impl sub-task 3).

The route ``GET /agents/<agent>/tool-results/<artifact>`` is the
cross-host transport for ``read_tool_result`` consumers: A2A peers,
MCP ``resources/read`` adapters, browsers, curl. These tests pin:

1. A real path_ref minted by MediaStore is fetchable via the route.
2. Path-traversal escapes are rejected (= 400, not silent leak).
3. Missing / deleted file → 404 (= ``not_found`` semantics aligned
   with ``read_tool_result``).
4. Unknown agent → 404 (= can't probe arbitrary paths via
   ``/agents/<garbage>/tool-results/<probe>``).
5. Content-Type derivation from the artifact extension (= text/plain
   for .txt, image/png for .png, application/octet-stream fallback).

Tier 2 because the route is the cross-host integrity boundary: a path-
traversal escape would expose arbitrary fs content; an unbounded agent
existence check would enable enumeration. Pin both invariants here so
any future refactor surfaces them at review.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Same path-bootstrap pattern as tests/web/test_smoke.py — keep the
# worktree src ahead of any editable-install collision.
_WORKTREE_SRC = Path(__file__).parent.parent.parent / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")
httpx = pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")


@pytest.fixture()
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Minimal Reyn project with one agent registered, deps cleared.

    Mirrors the smoke-test fixture so the FastAPI TestClient runs
    against a clean filesystem and the cached singletons in
    ``reyn.web.deps`` resolve to this tmp project.
    """
    reyn_dir = tmp_path / ".reyn"
    agents_dir = reyn_dir / "agents" / "researcher"
    agents_dir.mkdir(parents=True)

    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    (agents_dir / "profile.yaml").write_text(
        "name: researcher\nrole: ''\ncreated_at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )

    import reyn.web.deps as deps
    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None

    monkeypatch.setattr(
        "reyn.config._find_project_root", lambda _cwd: tmp_path,
    )
    # MediaStore reads from cwd; the route mints MediaStore with
    # ``project_root=Path.cwd()`` so the working dir must match for the
    # route's fs lookups to resolve in the test fixture.
    monkeypatch.chdir(tmp_path)
    yield tmp_path

    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None


def _client():
    from fastapi.testclient import TestClient

    from reyn.web.server import app
    return TestClient(app, raise_server_exceptions=False)


def _mint_path_ref(tmp_project: Path, content: str = "hello world\n") -> dict:
    """Use MediaStore to write a real path_ref under the tmp project."""
    from reyn.workspace.media_store import MediaStore, MediaStoreConfig
    store = MediaStore(
        MediaStoreConfig(),
        project_root=tmp_project,
        agent_name="researcher",
    )
    return store.save_tool_result(
        content, mime_type="text/plain", chain_id="chainX", tool="web_fetch", seq=1,
    )


# ── happy path ─────────────────────────────────────────────────────────


def test_get_serves_minted_path_ref_body(tmp_project: Path):
    """Tier 2: a path-ref minted by MediaStore is fetchable through the
    HTTP route — confirms the cross-host transport contract: minted
    URL → HTTP GET → original body.
    """
    block = _mint_path_ref(tmp_project, content="cross-host body\n")
    artifact = Path(block["path"]).name

    response = _client().get(f"/agents/researcher/tool-results/{artifact}")

    assert response.status_code == 200, response.text
    assert response.content == b"cross-host body\n"


def test_get_returns_text_plain_for_txt_artifact(tmp_project: Path):
    """Tier 2: ``Content-Type`` for ``.txt`` artifact is
    ``text/plain; charset=utf-8`` — lets browsers / HTTP clients render
    inline without download prompts.
    """
    block = _mint_path_ref(tmp_project, content="x")
    artifact = Path(block["path"]).name

    response = _client().get(f"/agents/researcher/tool-results/{artifact}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


# ── path-traversal protection ──────────────────────────────────────────


def test_get_rejects_dot_dot_traversal(tmp_project: Path):
    """Tier 2: a ``..`` escape in the artifact path is rejected with 400
    rather than reading outside the ``.reyn/tool-results/`` directory.
    Defends the cross-host transport against adversarial / malformed
    URLs minted by hostile peers.

    Even crafted as a single segment (= URL-encoded ``..``), the
    MediaStore boundary check catches the escape. FastAPI's router may
    pre-process some traversal patterns at the routing layer; this test
    pins the deepest layer (= MediaStore.read_tool_result raise) is
    reachable for any pattern that does get through.
    """
    # %2E%2E%2Fpasswd = "../passwd"; relies on FastAPI passing the raw
    # path segment to the handler so MediaStore sees the malformed input.
    response = _client().get(
        "/agents/researcher/tool-results/%2E%2E%2Fpasswd",
    )

    # Either 400 (= MediaStore boundary check caught it) or 404 (=
    # FastAPI / starlette refused before reaching the handler). Both
    # are acceptable "did not leak" outcomes; pin that it's NOT 200.
    assert response.status_code in (400, 404), (
        f"path traversal returned {response.status_code} — should be "
        f"400 or 404 to indicate refusal: {response.text}"
    )


# ── not_found semantics ────────────────────────────────────────────────


def test_get_missing_file_returns_404(tmp_project: Path):
    """Tier 2: a syntactically valid artifact name whose file doesn't
    exist returns 404 — matches ``MediaStore.read_tool_result``'s
    ``(b"", False)`` convention surfaced as HTTP semantics.
    """
    response = _client().get(
        "/agents/researcher/tool-results/20990101T000000-none-tool-1.txt",
    )

    assert response.status_code == 404


def test_get_unknown_agent_returns_404(tmp_project: Path):
    """Tier 2: an unregistered agent name returns 404 — prevents probing
    for arbitrary paths via ``/agents/<garbage>/tool-results/<probe>``
    even when the artifact filename is otherwise valid.
    """
    response = _client().get(
        "/agents/nonexistent/tool-results/any.txt",
    )

    assert response.status_code == 404


# ── router registration ────────────────────────────────────────────────


def test_resources_route_is_mounted_on_app():
    """Tier 2: the resources router is registered on the gateway. Without
    this, every other test in this module would still pass via 404 from
    FastAPI's default-not-found, masking a route-mount regression.
    """
    from reyn.web.server import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert any(
        "/agents/" in p and "/tool-results/" in p
        for p in paths
    ), f"resources route not mounted; routes seen: {sorted(paths)}"
