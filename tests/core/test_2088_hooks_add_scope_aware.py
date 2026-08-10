"""Tier 2: #2088 — scope-aware ``hooks_add`` (the per-agent write layer).

``hooks_add`` used to hardcode its write target to the GLOBAL runtime layer
(``.reyn/config/hooks.yaml``) regardless of which agent called it. This closes
the follow-up from #2073 S3 / #2073's per-agent-hooks add-on: a NAMED-agent
session's ``hooks_add`` call now writes that agent's OWN per-agent layer
(``.reyn/agents/<name>/hooks.yaml``) — the same path
``reyn.config.loader.load_per_agent_hooks`` / ``Session._read_per_agent_hooks``
already read from (operator-defined per-agent hooks have been read+combined
since #2073's per-agent-hooks add-on; only the WRITE side was missing). The
default/unnamed agent (``ctx.agent_name is None`` or ``== DEFAULT_AGENT_NAME``)
keeps writing the global layer — byte-identical to pre-#2088 behavior.

Precedence: the per-agent layer is ADDITIVE with the global layer (and the
startup + per-session layers) via ``Session._build_hook_registry`` — a 4-layer
COMBINE, not an override (see that method's docstring +
``docs/concepts/runtime/config-hot-reload.md``). This module verifies that by
observing BOTH a global-scoped and a per-agent-scoped hook fire on the same
session, per #2073's own precedent test style (no mocks — real Session /
HotReloader / EventLog / StateLog; a real ToolContext).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.config.loader import load_hot_reload_config, load_per_agent_hooks
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.runtime.hot_reload import HotReloader, set_active_hot_reloader
from reyn.runtime.registry import DEFAULT_AGENT_NAME
from reyn.tools.hooks import _handle_hooks_add
from reyn.tools.types import ToolContext
from tests._support.agent_session import make_session


@pytest.fixture(autouse=True)
def _reset_active_reloader():
    yield
    set_active_hot_reloader(None)


def _ctx(root: Path, *, agent_name: "str | None" = None) -> ToolContext:
    return ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(root=root), caller_kind="router",
        agent_name=agent_name,
    )


# ── Gate 1: the scoped write lands where the loader reads it ────────────────


@pytest.mark.asyncio
async def test_named_agent_write_lands_at_per_agent_path(tmp_path: Path) -> None:
    """Tier 2: a named-agent session's hooks_add writes
    .reyn/agents/<name>/hooks.yaml, NOT the global layer — and the REAL
    per-agent loader (load_per_agent_hooks) sees it. This is the gate that
    prevents a write-only feature: strip the scope branch in
    ``_hooks_yaml_path`` (make it always return the global path) and this
    goes RED (the per-agent file never exists; ``load_per_agent_hooks``
    returns [])."""
    set_active_hot_reloader(HotReloader(project_root=tmp_path, events=EventLog()))

    result = await _handle_hooks_add(
        {"on": "turn_end", "message": "my-own-hook"},
        _ctx(tmp_path, agent_name="scoped-agent"),
    )

    assert result["status"] == "ok"
    per_agent_path = tmp_path / ".reyn" / "agents" / "scoped-agent" / "hooks.yaml"
    global_path = tmp_path / ".reyn" / "config" / "hooks.yaml"
    assert per_agent_path.exists()
    assert not global_path.exists()  # did NOT leak into the global layer

    # Load through the REAL production read path (not a re-parse of the file
    # by hand) — the loader Session._read_per_agent_hooks itself calls.
    loaded = load_per_agent_hooks(tmp_path, "scoped-agent")
    assert any(h.get("template_push", {}).get("message") == "my-own-hook" for h in loaded)

    # And the real global-layer loader must NOT see it (no cross-scope leak).
    global_hooks = load_hot_reload_config(tmp_path).get("hooks", [])
    assert not any(
        h.get("template_push", {}).get("message") == "my-own-hook" for h in global_hooks
    )


@pytest.mark.asyncio
async def test_e2e_named_agent_hook_fires_via_real_session(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: full self-reload E2E for the per-agent scope (mirrors #2073's
    crown-jewel test) — a named agent's hooks_add write is picked up by ITS
    OWN Session._build_hook_registry COMBINE at the next turn boundary and
    the hook actually fires, observed via the public inbox. This is the
    strongest form of gate 1: the write is consumed by the real runtime
    dispatch path, not merely present on disk."""
    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="s2088-agent",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )

    ctx = ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(root=tmp_path), caller_kind="router",
        agent_name="s2088-agent", hot_reloader=session._hot_reloader,
    )
    result = await _handle_hooks_add(
        {"on": "turn_end", "message": "per-agent-fires", "wake": True}, ctx,
    )
    assert result["reload_scheduled"] is True
    assert str(result["path"]).endswith(
        str(Path(".reyn") / "agents" / "s2088-agent" / "hooks.yaml")
    )

    await session._hot_reloader.apply_pending()
    await session._hook_dispatcher.dispatch("turn_end", {})

    texts = set()
    while not session.inbox.empty():
        _kind, payload = session.inbox.get_nowait()
        texts.add(payload.get("text"))
    assert "per-agent-fires" in texts


# ── Gate 2: the unscoped/global path is unchanged ────────────────────────────


@pytest.mark.asyncio
async def test_no_agent_name_writes_global_unchanged(tmp_path: Path) -> None:
    """Tier 2: ctx.agent_name absent (non-session/test contexts, the pre-#2088
    default) — byte-identical to pre-#2088: writes .reyn/config/hooks.yaml."""
    set_active_hot_reloader(HotReloader(project_root=tmp_path, events=EventLog()))
    result = await _handle_hooks_add({"on": "turn_end", "message": "hi"}, _ctx(tmp_path))
    assert result["status"] == "ok"
    assert (tmp_path / ".reyn" / "config" / "hooks.yaml").exists()
    assert not (tmp_path / ".reyn" / "agents").exists()


@pytest.mark.asyncio
async def test_default_agent_name_writes_global(tmp_path: Path) -> None:
    """Tier 2: the default/unnamed agent (ctx.agent_name == DEFAULT_AGENT_NAME,
    the CLI's own default per chat.py) also writes the global layer, not
    .reyn/agents/default/hooks.yaml — the scope check is "named agent", not
    merely "agent_name is truthy"."""
    set_active_hot_reloader(HotReloader(project_root=tmp_path, events=EventLog()))
    result = await _handle_hooks_add(
        {"on": "turn_end", "message": "hi"},
        _ctx(tmp_path, agent_name=DEFAULT_AGENT_NAME),
    )
    assert result["status"] == "ok"
    assert (tmp_path / ".reyn" / "config" / "hooks.yaml").exists()
    assert not (tmp_path / ".reyn" / "agents" / DEFAULT_AGENT_NAME / "hooks.yaml").exists()


# ── Gate 3: precedence — additive, not override ──────────────────────────────


@pytest.mark.asyncio
async def test_global_and_per_agent_hooks_are_additive_not_overriding(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: precedence semantics, established from
    Session._build_hook_registry (startup ∪ runtime(global) ∪ per-agent ∪
    per-session — an ADDITIVE combine, see that method's docstring +
    docs/concepts/runtime/config-hot-reload.md's 3-layer COMBINE table): a
    global hook and a per-agent hook on the SAME lifecycle point BOTH fire —
    neither shadows the other. Falsify a "per-agent overrides global"
    (or vice versa) reading: if precedence were override-based, only one of
    the two texts would appear in the inbox."""
    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="precedence-agent",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )

    # Write the GLOBAL hook (default/unnamed scope).
    global_ctx = ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(root=tmp_path), caller_kind="router",
        hot_reloader=session._hot_reloader,
    )
    await _handle_hooks_add(
        {"on": "turn_end", "message": "global-hook", "wake": True}, global_ctx,
    )
    # Write the PER-AGENT hook (this session's own named scope).
    per_agent_ctx = ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(root=tmp_path), caller_kind="router",
        agent_name="precedence-agent", hot_reloader=session._hot_reloader,
    )
    await _handle_hooks_add(
        {"on": "turn_end", "message": "per-agent-hook", "wake": True}, per_agent_ctx,
    )

    await session._hot_reloader.apply_pending()
    await session._hook_dispatcher.dispatch("turn_end", {})

    texts = set()
    while not session.inbox.empty():
        _kind, payload = session.inbox.get_nowait()
        texts.add(payload.get("text"))
    assert "global-hook" in texts
    assert "per-agent-hook" in texts
