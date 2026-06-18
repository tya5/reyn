"""Tier 2: #1417 — the exec D14 visibility gate keys off the INJECTED sandbox
backend instance, not the reyn.yaml config string.

Construction-forwarding-gap fix: ``sandbox.backend=noop`` config + an injected
exec backend (``--env-backend=docker``) must still expose ``exec`` in
``list_actions(category=["exec"])`` because ``sandboxed_exec`` is functionally
available via the injected instance. Pins ``_exec_gate_backend_name`` (the
derivation) + ``visible_categories`` (the gate consuming it).
"""
from __future__ import annotations

from dataclasses import dataclass

from reyn.runtime.session import _exec_gate_backend_name
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import (
    _enumerate_category,
    is_exec_available,
    visible_categories,
)


def _router_ctx(sandbox_backend: str | None) -> ToolContext:
    """A minimal router ToolContext carrying the exec D14 gate value (the value
    session threads via _exec_gate_backend_name → RouterCallerState.sandbox_backend)."""
    return ToolContext(
        events=None,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(sandbox_backend=sandbox_backend),
    )


@dataclass
class _FakeBackend:
    """A sandbox/env backend instance exposing ``.name`` (like DockerEnvironment
    Backend.name='docker' / SandboxBackend.name='noop'|'seatbelt'|...)."""

    name: str


@dataclass
class _FakeSandboxConfig:
    backend: str


# ─── _exec_gate_backend_name: instance precedence over config string ──────────


def test_injected_instance_wins_over_noop_config() -> None:
    """Tier 2: #1417 — an injected docker backend + noop config → gate sees the
    instance ('docker'), not the config string ('noop'). The filed bug."""
    val = _exec_gate_backend_name(_FakeBackend(name="docker"), _FakeSandboxConfig(backend="noop"))
    assert val == "docker"
    assert is_exec_available(sandbox_backend=val) is True
    assert "exec" in visible_categories(sandbox_backend=val)


def test_injected_noop_instance_hidden() -> None:
    """Tier 2: #1417 — an injected noop backend → 'noop' → exec hidden, even if
    the config string says otherwise (instance is the truth)."""
    val = _exec_gate_backend_name(_FakeBackend(name="noop"), _FakeSandboxConfig(backend="docker"))
    assert val == "noop"
    assert is_exec_available(sandbox_backend=val) is False
    assert "exec" not in visible_categories(sandbox_backend=val)


def test_no_instance_falls_back_to_config() -> None:
    """Tier 2: #1417 — no injected instance → config string (auto/host-default
    behaviour unchanged)."""
    assert _exec_gate_backend_name(None, _FakeSandboxConfig(backend="docker")) == "docker"
    assert _exec_gate_backend_name(None, _FakeSandboxConfig(backend="noop")) == "noop"
    assert _exec_gate_backend_name(None, _FakeSandboxConfig(backend="auto")) == "auto"
    assert _exec_gate_backend_name(None, None) is None


def test_no_instance_noop_config_hidden_and_auto_visible() -> None:
    """Tier 2: #1417 — config-only path: noop hidden, auto visible (unchanged)."""
    noop_val = _exec_gate_backend_name(None, _FakeSandboxConfig(backend="noop"))
    auto_val = _exec_gate_backend_name(None, _FakeSandboxConfig(backend="auto"))
    assert "exec" not in visible_categories(sandbox_backend=noop_val)
    assert "exec" in visible_categories(sandbox_backend=auto_val)


def test_instance_without_name_degrades_to_hidden() -> None:
    """Tier 2: #1417 — a defensive: an injected instance lacking ``.name`` →
    None → exec hidden (the safe direction), never an AttributeError."""
    class _NoName:
        pass

    val = _exec_gate_backend_name(_NoName(), _FakeSandboxConfig(backend="noop"))
    assert val is None
    assert is_exec_available(sandbox_backend=val) is False


# ─── integration: the real list_actions exec-gate handler honors the value ────


def test_enumerate_exec_visible_with_docker_gate() -> None:
    """Tier 2: #1417 — the real `_enumerate_category('exec', ...)` handler returns
    exec__sandboxed_exec when the threaded gate value is a real backend ('docker',
    the value _exec_gate_backend_name derives from an injected docker instance even
    under noop config). Exercises the actual list_actions exec-gate path."""
    gate = _exec_gate_backend_name(_FakeBackend(name="docker"), _FakeSandboxConfig(backend="noop"))
    actions = _enumerate_category("exec", _router_ctx(gate))
    assert [a["qualified_name"] for a in actions] == ["exec__sandboxed_exec"]


def test_enumerate_exec_hidden_with_noop_gate() -> None:
    """Tier 2: #1417 — the handler returns [] (exec hidden) when the gate is noop
    (no injected instance + noop config)."""
    gate = _exec_gate_backend_name(None, _FakeSandboxConfig(backend="noop"))
    assert _enumerate_category("exec", _router_ctx(gate)) == []
