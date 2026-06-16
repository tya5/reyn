"""Smoke tests for PR30 — OpenUI shell, /api/web/config, /api/web/data,
design file serving, and the --default-design CLI flag.

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
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_project(tmp_path: Path) -> Path:
    """Minimal project root with one agent, one local design, and reyn.yaml."""
    # reyn.yaml
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")

    # Agent
    agents_dir = tmp_path / ".reyn" / "agents" / "default"
    agents_dir.mkdir(parents=True)
    (agents_dir / "profile.yaml").write_text(
        "name: default\nrole: ''\ncreated_at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )

    # Local design: "coral"
    coral_dir = tmp_path / "reyn" / "local" / "designs" / "coral"
    coral_dir.mkdir(parents=True)
    (coral_dir / "design.yaml").write_text(
        "schema: reyn-ui/v1\nfaces:\n  - app\n  - studio\n",
        encoding="utf-8",
    )
    (coral_dir / "Reyn.html").write_text(
        "<html><body><div id='app'>hello coral</div></body></html>",
        encoding="utf-8",
    )

    # Stdlib-equivalent design: "warm" under web/designs/
    warm_dir = tmp_path / "web" / "designs" / "warm"
    warm_dir.mkdir(parents=True)
    (warm_dir / "Reyn.html").write_text(
        "<html><body><div id='app'>hello warm</div></body></html>",
        encoding="utf-8",
    )

    return tmp_path


def _make_client(tmp_project: Path, monkeypatch, env_overrides: dict | None = None):
    """Build a TestClient with singletons reset to use tmp_project."""

    from fastapi.testclient import TestClient

    import reyn.interfaces.web.deps as deps

    # Reset cached singletons
    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None

    monkeypatch.setattr("reyn.config._find_project_root", lambda _: tmp_project)

    if env_overrides:
        for k, v in env_overrides.items():
            monkeypatch.setenv(k, v)
    else:
        monkeypatch.delenv("REYN_WEB_DEFAULT_DESIGN", raising=False)

    from reyn.interfaces.web.server import app
    client = TestClient(app, raise_server_exceptions=False)
    return client


def _cleanup(monkeypatch):
    import reyn.interfaces.web.deps as deps
    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None


# ---------------------------------------------------------------------------
# 1. Shell HTML — GET /
# ---------------------------------------------------------------------------

def test_shell_root_redirects(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2c: GET / returns a redirect (302) to /static/index.html."""
    client = _make_client(tmp_project, monkeypatch)
    try:
        # follow_redirects=False so we can inspect the redirect itself
        response = client.get("/", follow_redirects=False)
        assert response.status_code in (302, 307), (
            f"Expected redirect, got {response.status_code}: {response.text[:200]}"
        )
    finally:
        _cleanup(monkeypatch)


def test_shell_static_index_200(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2c: GET /static/index.html returns 200 with HTML content."""
    client = _make_client(tmp_project, monkeypatch)
    try:
        response = client.get("/static/index.html")
        assert response.status_code == 200, (
            f"/static/index.html returned {response.status_code}: {response.text[:200]}"
        )
        assert "OPENUI_HOST" in response.text, "Shell HTML must define OPENUI_HOST"
        assert "OPENUI_DATA" in response.text, "Shell HTML must reference OPENUI_DATA"
        assert "OPENUI_SCHEMA" in response.text, "Shell HTML must set OPENUI_SCHEMA"
    finally:
        _cleanup(monkeypatch)


# ---------------------------------------------------------------------------
# 2. /api/web/config
# ---------------------------------------------------------------------------

def test_web_config_200_shape(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2c: GET /api/web/config returns 200 with required keys."""
    client = _make_client(tmp_project, monkeypatch)
    try:
        response = client.get("/api/web/config")
        assert response.status_code == 200, (
            f"/api/web/config returned {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "default_design" in data
        assert "schemas_supported" in data
        assert "available_designs" in data
        assert isinstance(data["schemas_supported"], list)
        assert isinstance(data["available_designs"], list)
        assert "reyn-ui/v1" in data["schemas_supported"]
    finally:
        _cleanup(monkeypatch)


def test_web_config_lists_local_design(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2b: available_designs includes the local coral design."""
    client = _make_client(tmp_project, monkeypatch)
    try:
        data = client.get("/api/web/config").json()
        slugs = [d["slug"] for d in data["available_designs"]]
        assert "coral" in slugs, f"Expected 'coral' in {slugs}"
        coral = next(d for d in data["available_designs"] if d["slug"] == "coral")
        assert coral["source"] == "local"
        assert coral["schema"] == "reyn-ui/v1"
    finally:
        _cleanup(monkeypatch)


def test_web_config_default_design_env(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2b: REYN_WEB_DEFAULT_DESIGN env var is reflected in default_design."""
    client = _make_client(tmp_project, monkeypatch, env_overrides={"REYN_WEB_DEFAULT_DESIGN": "coral"})
    try:
        data = client.get("/api/web/config").json()
        assert data["default_design"] == "coral", f"Got {data['default_design']}"
    finally:
        _cleanup(monkeypatch)


def test_web_config_project_overrides_stdlib(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2b: Project-level design slug takes priority over stdlib slug."""
    # Add a project-level "warm" design (same slug as web/designs/warm)
    proj_warm = tmp_project / "reyn" / "project" / "designs" / "warm"
    proj_warm.mkdir(parents=True)
    (proj_warm / "design.yaml").write_text("schema: reyn-ui/v1\n", encoding="utf-8")

    client = _make_client(tmp_project, monkeypatch)
    try:
        data = client.get("/api/web/config").json()
        warm_entries = [d for d in data["available_designs"] if d["slug"] == "warm"]
        # Exactly one "warm" entry after deduplication — project source wins
        assert warm_entries, f"Expected a warm entry, got {warm_entries}"
        assert warm_entries[0]["source"] == "project"
    finally:
        _cleanup(monkeypatch)


# ---------------------------------------------------------------------------
# 3. /api/web/data
# ---------------------------------------------------------------------------

def test_web_data_200_shape(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2c: GET /api/web/data returns 200 with required ReynUiData keys."""
    client = _make_client(tmp_project, monkeypatch)
    try:
        response = client.get("/api/web/data")
        assert response.status_code == 200, (
            f"/api/web/data returned {response.status_code}: {response.text}"
        )
        data = response.json()
        for key in ("AGENTS", "RECAP", "QUICKSTARTS", "LIBRARY", "COPY",
                    "CONVO_ARIA", "CONVO_ARIA_STUDIO", "SKILL_GRAPH",
                    "RUN_EVENTS", "RUNS_LIST", "PERMISSIONS"):
            assert key in data, f"Missing key {key!r} in /api/web/data response"
    finally:
        _cleanup(monkeypatch)


def test_web_data_agents_list(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2b: AGENTS field contains the 'default' agent from the tmp project."""
    client = _make_client(tmp_project, monkeypatch)
    try:
        data = client.get("/api/web/data").json()
        assert isinstance(data["AGENTS"], list)
        ids = [a["id"] for a in data["AGENTS"]]
        assert "default" in ids, f"Expected 'default' in AGENTS {ids}"
    finally:
        _cleanup(monkeypatch)


def test_web_data_copy_keys(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2b: COPY has 'en' and 'ja' sub-objects with required I18nKeys."""
    client = _make_client(tmp_project, monkeypatch)
    try:
        data = client.get("/api/web/data").json()
        copy = data["COPY"]
        assert "en" in copy and "ja" in copy
        required_keys = ["today_morning", "send", "placeholder", "agents", "studio", "library"]
        for lang in ("en", "ja"):
            for k in required_keys:
                assert k in copy[lang], f"COPY[{lang!r}] missing key {k!r}"
    finally:
        _cleanup(monkeypatch)


def test_web_data_quickstarts(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2b: QUICKSTARTS is a non-empty list with id/icon/title/sub fields."""
    client = _make_client(tmp_project, monkeypatch)
    try:
        data = client.get("/api/web/data").json()
        qs = data["QUICKSTARTS"]
        assert len(qs) > 0
        for item in qs:
            for field in ("id", "icon", "title", "sub"):
                assert field in item, f"Quickstart missing {field!r}: {item}"
    finally:
        _cleanup(monkeypatch)


# ---------------------------------------------------------------------------
# 4. Design file serving — /web/designs/{slug}/{file}
# ---------------------------------------------------------------------------

def test_design_file_200(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2c: GET /web/designs/coral/Reyn.html returns the design's Reyn.html."""
    client = _make_client(tmp_project, monkeypatch)
    try:
        response = client.get("/web/designs/coral/Reyn.html")
        assert response.status_code == 200, (
            f"/web/designs/coral/Reyn.html returned {response.status_code}"
        )
        assert "hello coral" in response.text
    finally:
        _cleanup(monkeypatch)


def test_design_file_404(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2b: GET /web/designs/nonexistent/foo.js returns 404."""
    client = _make_client(tmp_project, monkeypatch)
    try:
        response = client.get("/web/designs/nonexistent/foo.js")
        assert response.status_code == 404
    finally:
        _cleanup(monkeypatch)


def test_design_file_stdlib_warm(tmp_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2c: GET /web/designs/warm/Reyn.html serves the web/designs/warm/Reyn.html file."""
    client = _make_client(tmp_project, monkeypatch)
    try:
        response = client.get("/web/designs/warm/Reyn.html")
        assert response.status_code == 200
        assert "hello warm" in response.text
    finally:
        _cleanup(monkeypatch)


# ---------------------------------------------------------------------------
# 5. CLI --default-design flag
# ---------------------------------------------------------------------------

def test_web_help_includes_default_design() -> None:
    """Tier 2b: `reyn web --help` mentions --default-design."""
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
    assert "default-design" in result.stdout.lower(), (
        f"Expected --default-design in help, got:\n{result.stdout}"
    )
