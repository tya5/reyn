"""Smoke tests for the reyn.web gateway.

These tests verify:
1. The FastAPI app can be imported.
2. `reyn web --help` exits cleanly.
3. Basic REST endpoints return 200 against a temporary project root.

Tests do NOT start a real server or require a live LLM — they use
FastAPI's TestClient for synchronous in-process HTTP.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Ensure the worktree src is importable even when the main-tree src appears
# earlier in sys.path (editable-install collision).
# ---------------------------------------------------------------------------
_WORKTREE_SRC = Path(__file__).parent.parent.parent / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

# ---------------------------------------------------------------------------
# Guard: skip all tests if fastapi / httpx (TestClient dep) not installed.
# ---------------------------------------------------------------------------
fastapi = pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")
httpx = pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")


# ---------------------------------------------------------------------------
# 1. Import smoke test
# ---------------------------------------------------------------------------

def test_app_import() -> None:
    """The FastAPI app can be imported from reyn.web.server."""
    from reyn.web.server import app
    assert app is not None
    assert app.title == "Reyn Web Gateway"


def test_routers_mounted() -> None:
    """All expected routers are included in the app."""
    from reyn.web.server import app
    routes = {r.path for r in app.routes}
    # Basic sanity: key prefixes exist
    assert any("/api/agents" in r for r in routes), f"No /api/agents route in {routes}"
    assert any("/api/skills" in r for r in routes), f"No /api/skills route in {routes}"
    assert any("/api/runs" in r for r in routes), f"No /api/runs route in {routes}"
    assert any("/health" in r for r in routes), f"No /health route in {routes}"
    assert any("/ws/chat/" in r for r in routes), f"No /ws/chat/ route in {routes}"


# ---------------------------------------------------------------------------
# 2. CLI smoke test
# ---------------------------------------------------------------------------

def test_reyn_web_help() -> None:
    """`reyn web --help` exits cleanly (exit code 0, stdout contains usage).

    Uses the reyn._cli:main entry point directly so we don't depend on the
    installed `reyn` shim, which may point to the wrong editable install.
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "from reyn._cli import main; import sys; sys.argv=['reyn','web','--help']; main()"],
        capture_output=True,
        text=True,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(_WORKTREE_SRC),
        },
        timeout=15,
    )
    assert result.returncode == 0, f"reyn web --help failed:\n{result.stderr}"
    assert "host" in result.stdout.lower() or "port" in result.stdout.lower(), (
        f"Expected help text, got:\n{result.stdout}"
    )


# ---------------------------------------------------------------------------
# 3. REST endpoint smoke test (GET /api/agents against a tmp project root)
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_project(tmp_path: Path) -> Path:
    """Create a minimal .reyn/agents/default/ directory tree.

    The gateway discovers project_root via _find_project_root(cwd), which looks
    for reyn.yaml or .reyn/. We plant both so discovery works.
    """
    reyn_dir = tmp_path / ".reyn"
    agents_dir = reyn_dir / "agents" / "default"
    agents_dir.mkdir(parents=True)

    # Minimal reyn.yaml so _find_project_root() matches this directory.
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")

    # Minimal agent profile.
    (agents_dir / "profile.yaml").write_text(
        "name: default\nrole: ''\ncreated_at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    return tmp_path


def test_get_agents_200(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/agents returns 200 and a list containing 'default'."""
    from fastapi.testclient import TestClient

    # Patch _find_project_root and the lru_cache-based singletons so the test
    # uses our tmp_project directory instead of the real project root.
    import reyn.web.deps as deps

    # Clear cached singletons from previous test runs.
    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None

    monkeypatch.setattr(
        "reyn.config._find_project_root",
        lambda _cwd: tmp_project,
    )

    from reyn.web.server import app
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/agents")

    # Restore cached state for other tests.
    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None

    assert response.status_code == 200, (
        f"GET /api/agents returned {response.status_code}:\n{response.text}"
    )
    data = response.json()
    assert isinstance(data, list)
    names = [a["name"] for a in data]
    assert "default" in names, f"Expected 'default' agent in {names}"


def test_health_endpoint() -> None:
    """GET /health returns {status: ok}."""
    from fastapi.testclient import TestClient

    from reyn.web.server import app

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
