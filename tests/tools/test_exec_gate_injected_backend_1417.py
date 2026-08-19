"""Tier 2: #1417 — the exec isolation-disclosure value keys off the INJECTED
sandbox backend instance, not the reyn.yaml config string.

Construction-forwarding-gap fix: ``sandbox.backend=noop`` config + an injected
exec backend (``--env-backend=docker``) must still be treated as ISOLATED
(disclosure text absent) because ``exec`` actually runs via the injected
instance, not the config string. Pins ``_exec_gate_backend_name`` (the
derivation) + ``is_exec_isolated``/``_enumerate_category`` (the consumers).

#4932 (owner ruling, 2026-08-19): exec's VISIBILITY no longer depends on
this value at all — ``exec`` is always enumerated (see
``test_universal_catalog.py``'s own #4932 tests for that side). This file
narrows to what #1417 was actually about: the derived backend NAME must be
the injected instance's, not the config string's, because that name is
what feeds the isolation-disclosure text now (get the wrong name here and
the disclosure lies about which backend is really running commands).
"""
from __future__ import annotations

from dataclasses import dataclass

from reyn.runtime.session import _exec_gate_backend_name
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_catalog import (
    _enumerate_category,
    is_exec_isolated,
)


def _router_ctx(sandbox_backend: str | None) -> ToolContext:
    """A minimal router ToolContext carrying the exec gate value (the value
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
    assert is_exec_isolated(sandbox_backend=val) is True


def test_injected_noop_instance_not_isolated() -> None:
    """Tier 2: #1417 — an injected noop backend → 'noop' → not isolated, even if
    the config string says otherwise (instance is the truth)."""
    val = _exec_gate_backend_name(_FakeBackend(name="noop"), _FakeSandboxConfig(backend="docker"))
    assert val == "noop"
    assert is_exec_isolated(sandbox_backend=val) is False


def test_no_instance_falls_back_to_config() -> None:
    """Tier 2: #1417 — no injected instance → config string (auto/host-default
    behaviour unchanged)."""
    assert _exec_gate_backend_name(None, _FakeSandboxConfig(backend="docker")) == "docker"
    assert _exec_gate_backend_name(None, _FakeSandboxConfig(backend="noop")) == "noop"
    assert _exec_gate_backend_name(None, _FakeSandboxConfig(backend="auto")) == "auto"
    assert _exec_gate_backend_name(None, None) is None


def test_no_instance_noop_config_not_isolated_and_auto_isolated() -> None:
    """Tier 2: #1417 — config-only path: noop not isolated, auto isolated
    (unchanged derivation; only the CONSUMER — availability vs disclosure —
    changed in #4932)."""
    noop_val = _exec_gate_backend_name(None, _FakeSandboxConfig(backend="noop"))
    auto_val = _exec_gate_backend_name(None, _FakeSandboxConfig(backend="auto"))
    assert is_exec_isolated(sandbox_backend=noop_val) is False
    assert is_exec_isolated(sandbox_backend=auto_val) is True


def test_instance_without_name_degrades_to_not_isolated() -> None:
    """Tier 2: #1417 — a defensive: an injected instance lacking ``.name`` →
    None → not isolated (the safe direction for the disclosure text), never
    an AttributeError."""
    class _NoName:
        pass

    val = _exec_gate_backend_name(_NoName(), _FakeSandboxConfig(backend="noop"))
    assert val is None
    assert is_exec_isolated(sandbox_backend=val) is False


# ─── integration: the real list_actions exec-gate handler honors the value ────


def test_enumerate_exec_no_disclosure_with_docker_gate() -> None:
    """Tier 2: #1417 — the real `_enumerate_category('exec', ...)` handler
    returns exec with NO isolation-disclosure suffix when the threaded gate
    value is a real backend ('docker', the value _exec_gate_backend_name
    derives from an injected docker instance even under noop config)."""
    gate = _exec_gate_backend_name(_FakeBackend(name="docker"), _FakeSandboxConfig(backend="noop"))
    actions = _enumerate_category("exec", _router_ctx(gate))
    assert [a["action_name"] for a in actions] == ["exec"]
    assert "no sandbox isolation" not in actions[0]["short_description"]


def test_enumerate_exec_discloses_no_isolation_with_noop_gate() -> None:
    """Tier 2: #1417/#4932 — exec is still enumerated (never hidden) when the
    gate is noop (no injected instance + noop config), but its description
    discloses the absence of isolation instead."""
    gate = _exec_gate_backend_name(None, _FakeSandboxConfig(backend="noop"))
    actions = _enumerate_category("exec", _router_ctx(gate))
    assert [a["action_name"] for a in actions] == ["exec"]
    assert "no sandbox isolation" in actions[0]["short_description"]
