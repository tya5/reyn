"""Tier 2: FP-0058 P2 SurfaceSpec registry — opt-in/opt-out surface mounting.

Before this module, ``server.py`` mounted every core surface (AG-UI, the
OpenUI web shell, ``/health``, the REST ``/api`` control plane, resources,
A2A, MCP) unconditionally. The ``SurfaceSpec`` registry
(``reyn.interfaces.web.surfaces``) makes A2A and MCP opt-in (secure-default
OFF) and every surface's mount decision goes through one seam:
``resolve_enabled`` (CLI > ``web.surfaces`` config > secure-default) gates
whether ``mount(app, config)`` is even called.

Reachability strip-gate (the load-bearing assertion,
``test_disabled_surface_returns_404_enabled_surface_is_reachable``): with the
enabled-check present, a disabled surface (A2A on a fresh install) 404s and
an enabled one (``/health``) is reachable. Deleting ``mount_all``'s
``if not enabled: continue`` guard makes ``mount()`` run unconditionally for
every surface, so the disabled surface would mount anyway and the 404
assertion goes RED — that is the proof the enabled-check, not something
else, keeps an opt-in surface unreachable. (404 is checked WITH a valid
auth token, isolating "not mounted" from the separate P1 401 auth gate,
which gates by path prefix regardless of whether a route exists behind it.)

Each scenario needs the app mounted under a DIFFERENT surface configuration
(default / config-override / CLI-override), so each test force-reimports
``reyn.interfaces.web.server`` fresh (the module mounts surfaces once, at
import time) rather than reusing the process-wide singleton the other web
tests share.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")
httpx = pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")

from fastapi.testclient import TestClient  # noqa: E402

_ENABLE_ENV = "REYN_WEB_ENABLE_SURFACES"
_DISABLE_ENV = "REYN_WEB_DISABLE_SURFACES"


@pytest.fixture(autouse=True)
def _restore_server_singleton():
    """Every test in this module force-reimports ``reyn.interfaces.web.server``
    against a throwaway project root / env override (see ``_fresh_app``).
    Left alone, the LAST such reimport would stay parked in ``sys.modules``
    for the rest of the pytest session — any other test file's plain
    ``from reyn.interfaces.web.server import app`` would silently pick up
    THIS module's scoped app (built against a tmp project root and possibly
    a2a/mcp forced off) instead of a normal one. Popping it here, at
    teardown, means the next consumer's own import re-mounts fresh against
    whatever env is actually active at that point (the repo's real project
    root, the session-default ``REYN_WEB_ENABLE_SURFACES=a2a,mcp`` from the
    root ``conftest.py``) — the surface-registry tests stay fully isolated
    from the rest of the suite.
    """
    yield
    import sys
    for name in ("reyn.interfaces.web.server", "reyn.interfaces.web.surfaces"):
        sys.modules.pop(name, None)


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


def _write_surfaces_config(project_root: Path, overrides: dict) -> None:
    """Append a ``web: surfaces:`` block to the project's reyn.yaml."""
    import yaml
    lines = ["web:", "  surfaces:"]
    for name, enabled in overrides.items():
        lines.append(f"    {name}:")
        lines.append(f"      enabled: {str(bool(enabled)).lower()}")
    text = (project_root / "reyn.yaml").read_text(encoding="utf-8")
    (project_root / "reyn.yaml").write_text(text + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    del yaml  # only imported to fail loudly if pyyaml is ever missing in this env


def _fresh_app(
    monkeypatch: pytest.MonkeyPatch,
    project_root: Path,
    *,
    enable: str = "",
    disable: str = "",
):
    """Force a clean re-import of ``reyn.interfaces.web.server`` so ``mount_all``
    re-resolves against the given project root + CLI-override env vars.

    ``reyn.config.loader._find_project_root`` is patched directly (the module
    ``load_config`` is defined in, resolving its own bare-name global at call
    time) rather than the ``reyn.config`` package re-export — the package copy
    is a separate name binding that ``load_config``'s internal call does not
    read through.
    """
    import reyn.config.loader as _loader
    monkeypatch.setattr(_loader, "_find_project_root", lambda _cwd: project_root)
    monkeypatch.setenv(_ENABLE_ENV, enable)
    monkeypatch.setenv(_DISABLE_ENV, disable)

    # Also steer reyn.interfaces.web.deps' own project-root/config lookups
    # (a separate lazy-import call site) so any handler that reads them
    # (e.g. /api/agents) resolves against the same tmp project.
    monkeypatch.setattr("reyn.config._find_project_root", lambda _cwd: project_root)
    import reyn.interfaces.web.deps as deps
    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None

    for name in [
        "reyn.interfaces.web.server",
        "reyn.interfaces.web.surfaces",
    ]:
        sys.modules.pop(name, None)
    module = importlib.import_module("reyn.interfaces.web.server")
    return module.app


def _client_with_token(app):
    """Real ``TestClient`` + the token the real lifespan-built ``AuthContext``
    generated (network tier — the default TestClient host classifies as
    cross-machine, so a request with no token is 401 regardless of surface
    enablement; presenting the token isolates "not mounted" (404) from
    "not authenticated" (401))."""
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()  # fire the lifespan so app.state.auth exists
    return client, client.app.state.auth.token


# ── 1. Reachability strip-gate ────────────────────────────────────────────


def test_disabled_surface_returns_404_enabled_surface_is_reachable(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a2a/mcp stay unmounted (404) on a fresh install; health/webui/api
    are reachable. See the module docstring for the strip-gate falsification."""
    app = _fresh_app(monkeypatch, tmp_project)
    client, token = _client_with_token(app)
    try:
        # Enabled surfaces: reachable (not 404).
        assert client.get("/health").status_code == 200
        assert client.get("/", follow_redirects=False).status_code == 302
        assert client.get(f"/api/agents?token={token}").status_code == 200

        # Disabled-by-default surfaces: unmounted (404), even WITH a valid
        # token — proves it is "not mounted", not "unauthenticated".
        assert client.post(f"/a2a/agents/default?token={token}", json={}).status_code == 404
        assert client.post(f"/mcp/messages?token={token}").status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_enabling_a2a_makes_it_reachable(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: opting A2A in (CLI --enable) mounts it — no longer 404."""
    app = _fresh_app(monkeypatch, tmp_project, enable="a2a")
    client, token = _client_with_token(app)
    try:
        resp = client.post(f"/a2a/agents/default?token={token}", json={})
        assert resp.status_code != 404
    finally:
        client.__exit__(None, None, None)


# ── 2. CLI > config > secure-default precedence ───────────────────────────


def test_cli_disable_beats_config_enabled_true(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: web.surfaces config enables a2a, but --disable a2a still wins."""
    _write_surfaces_config(tmp_project, {"a2a": True})
    app = _fresh_app(monkeypatch, tmp_project, disable="a2a")
    client, token = _client_with_token(app)
    try:
        resp = client.post(f"/a2a/agents/default?token={token}", json={})
        assert resp.status_code == 404, (
            "config enabled a2a but --disable a2a must still win (CLI > config)"
        )
    finally:
        client.__exit__(None, None, None)


def test_config_enabled_beats_secure_default_off(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: with no CLI override, web.surfaces config enabling a2a beats the
    OFF secure-default."""
    _write_surfaces_config(tmp_project, {"a2a": True})
    app = _fresh_app(monkeypatch, tmp_project)
    client, token = _client_with_token(app)
    try:
        resp = client.post(f"/a2a/agents/default?token={token}", json={})
        assert resp.status_code != 404, (
            "config enabled a2a with no CLI override must mount it (config > secure-default)"
        )
    finally:
        client.__exit__(None, None, None)


# ── 3. Fresh-install ON-set ────────────────────────────────────────────────


def test_fresh_install_default_on_set_excludes_a2a_and_mcp(
    tmp_project: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a project with no web.surfaces config and no CLI overrides gets
    exactly the owner-locked ON set (agui/webui/health/api/resources) —
    A2A and MCP stay off."""
    from reyn.interfaces.web.surfaces import build_registry
    app = _fresh_app(monkeypatch, tmp_project)
    client, token = _client_with_token(app)
    try:
        on_by_default = {s.name for s in build_registry() if s.default_enabled}
        off_by_default = {s.name for s in build_registry() if not s.default_enabled}
        assert on_by_default == {"api", "resources", "agui", "health", "webui"}
        assert off_by_default == {"a2a", "mcp"}

        assert client.get("/health").status_code == 200
        assert client.post(f"/a2a/agents/default?token={token}", json={}).status_code == 404
        assert client.post(f"/mcp/messages?token={token}").status_code == 404
    finally:
        client.__exit__(None, None, None)
