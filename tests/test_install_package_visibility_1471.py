"""Tier 2: #1471 — mcp__install_package hot-list visibility + not-found guidance.

Two invariants:

1. DEFAULT_HOT_LIST_SEED contains mcp__install_package — pins visibility parity
   with mcp__install_registry (previously install_package was only reachable via
   list_actions, causing plan-driven weak models to always grab install_registry).

2. mcp__install_registry not-found (HTTP 404) error carries decision-enabling
   guidance pointing to mcp__install_package — pins the LLM-visible error data
   so the model can immediately pivot without a list_actions round-trip.

No mocks. Network-doing RegistryClient is replaced via monkeypatch.setattr with
a real fake class (real async context manager, real get_server coroutine method
that raises RegistryError — same sanctioned seam used in test_mcp_install_workspace
1442.py). `ctx` is a minimal real ToolContext (no workspace needed on the 404 path
— ctx is not accessed before the RegistryClient call).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import EventLog
from reyn.core.registry.client import RegistryError
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import MCPInstallIROp
from reyn.tools.types import RouterCallerState, ToolContext

# ── 1. Seed contains install_package ────────────────────────────────────────


def test_install_package_in_default_hot_list_seed() -> None:
    """Tier 2: #1471 — mcp__install_package must be in DEFAULT_HOT_LIST_SEED.
    Regression pin: removing it would reintroduce the visibility asymmetry."""
    from reyn.tools.action_usage_tracker import DEFAULT_HOT_LIST_SEED
    assert "mcp__install_package" in DEFAULT_HOT_LIST_SEED, (
        "mcp__install_package must be in DEFAULT_HOT_LIST_SEED for hot-list "
        "visibility parity with mcp__install_registry"
    )


def test_install_registry_also_in_seed() -> None:
    """Tier 2: #1471 — mcp__install_registry must remain in seed (regression
    pin: adding install_package must not accidentally remove install_registry)."""
    from reyn.tools.action_usage_tracker import DEFAULT_HOT_LIST_SEED
    assert "mcp__install_registry" in DEFAULT_HOT_LIST_SEED


def test_hot_list_n_default_is_zero() -> None:
    """Tier 2: #1471 → default-flip — ActionRetrievalConfig.hot_list_n default
    is 0 (off). The seed and mechanism remain intact for opt-in (hot_list_n: 10+
    in reyn.yaml). Regression pin against accidental revert to non-zero default."""
    from reyn.config import ActionRetrievalConfig
    assert ActionRetrievalConfig().hot_list_n == 0


# ── 2. Not-found error carries install_package guidance ─────────────────────


class _RegistryClientNotFound:
    """Real fake RegistryClient that always raises HTTP 404 on get_server.

    Used via monkeypatch.setattr on reyn.core.registry.client.RegistryClient so
    mcp_install.handle's local import resolves to this class without touching
    the real network.  No AsyncMock — pure subclass with real coroutine methods.
    """

    async def __aenter__(self) -> "_RegistryClientNotFound":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def get_server(self, server_id: str) -> None:
        raise RegistryError(f"HTTP 404: server '{server_id}' not found in registry")


class _RegistryClientNetworkError:
    """Real fake RegistryClient that raises a non-404 network error."""

    async def __aenter__(self) -> "_RegistryClientNetworkError":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def get_server(self, server_id: str) -> None:
        raise RegistryError("Registry unreachable: Connection refused")


def _minimal_ctx(tmp_path: "Path | None" = None) -> ToolContext:
    """Minimal ToolContext for the not-found path (ctx is not accessed before
    the RegistryClient call, so a bare context is sufficient)."""
    from pathlib import Path
    base = tmp_path or Path("/tmp")
    events = EventLog()
    return ToolContext(
        events=events,
        permission_resolver=None,
        workspace=Workspace(events=events, base_dir=base),
        caller_kind="router",
        router_state=RouterCallerState(),
    )


def _make_registry_op(server_id: str) -> MCPInstallIROp:
    return MCPInstallIROp(
        kind="mcp_install",
        server_id=server_id,
        scope="local",
        env_overrides=None,
        source=None,  # registry path
        extra_args=None,
    )


@pytest.mark.asyncio
async def test_not_found_error_mentions_install_package(tmp_path, monkeypatch) -> None:
    """Tier 2: #1471 — when install_registry gets HTTP 404 (server not in
    registry), the error data must mention mcp__install_package so the LLM can
    immediately pivot without a list_actions round-trip."""
    import reyn.core.registry.client as _rc
    monkeypatch.setattr(_rc, "RegistryClient", _RegistryClientNotFound)

    from reyn.core.op_runtime import mcp_install as _mi
    result = await _mi.handle(_make_registry_op("some-npm-package"), _minimal_ctx(tmp_path), caller="control_ir")

    assert result["status"] == "error"
    error_text = result["error"]
    assert "mcp__install_package" in error_text, (
        f"not-found error must mention mcp__install_package; got: {error_text!r}"
    )


@pytest.mark.asyncio
async def test_not_found_error_mentions_source_param(tmp_path, monkeypatch) -> None:
    """Tier 2: #1471 — the not-found guidance must include source= so the LLM
    knows the required parameter name for mcp__install_package."""
    import reyn.core.registry.client as _rc
    monkeypatch.setattr(_rc, "RegistryClient", _RegistryClientNotFound)

    from reyn.core.op_runtime import mcp_install as _mi
    result = await _mi.handle(_make_registry_op("mypackage"), _minimal_ctx(tmp_path), caller="control_ir")

    assert result["status"] == "error"
    assert "source" in result["error"], (
        "guidance must name the 'source=' parameter of mcp__install_package"
    )


@pytest.mark.asyncio
async def test_non_404_error_does_not_get_install_package_guidance(
    tmp_path, monkeypatch
) -> None:
    """Tier 2: #1471 — a non-404 RegistryError (network failure) must NOT get
    the install_package guidance. The guidance is specific to HTTP 404."""
    import reyn.core.registry.client as _rc
    monkeypatch.setattr(_rc, "RegistryClient", _RegistryClientNetworkError)

    from reyn.core.op_runtime import mcp_install as _mi
    result = await _mi.handle(_make_registry_op("someserver"), _minimal_ctx(tmp_path), caller="control_ir")

    assert result["status"] == "error"
    error_text = result["error"]
    assert "Registry fetch failed" in error_text
    assert "mcp__install_package" not in error_text
