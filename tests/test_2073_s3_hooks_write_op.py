"""Tier 2: #2073 S3 — the LLM-op hooks-write self-reload trigger (crown-jewel).

`hooks_add` lets the agent add a runtime push hook: it writes the FIXED
.reyn/hooks.yaml (the IN-set runtime layer) + request_reload(source="llm_op") → the
HotReloader applies the S2b hooks seam at the turn boundary. The write-gate is
structural: the tool takes hook CONTENT, never a path, so it CANNOT aim at reyn.yaml
(the restart-only OUT-set). Permission is the TOOL axis (require_tool + the #2074
capability profile) — exercised at router dispatch, not in this handler.

No mocks: real HotReloader / EventLog / Session / load_hooks; a real ToolContext
(permission_resolver=None → the file-write gate is a no-op in-test, the standard
tool-test contract). The crown-jewel E2E runs the full agent-adds-hook → reload →
hook-fires path on a real Session, observed via the public inbox.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.runtime.hot_reload import HotReloader, set_active_hot_reloader
from reyn.runtime.session import Session
from reyn.tools.hooks import HOOKS_ADD, _handle_hooks_add
from reyn.tools.types import ToolContext
from tests._support.agent_session import make_session


@pytest.fixture(autouse=True)
def _reset_active_reloader():
    """Reset the process-wide active reloader after each test (it's a global)."""
    yield
    set_active_hot_reloader(None)


def _ctx(root: Path) -> ToolContext:
    return ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(root=root), caller_kind="router",
    )


# ── write-gate BY CONSTRUCTION (the crown-jewel falsify) ────────────────────


def test_hooks_add_has_no_path_parameter() -> None:
    """Tier 2: the tool takes hook CONTENT, never a path — so it is structurally
    impossible to aim the write at reyn.yaml; the target is the hardcoded
    .reyn/hooks.yaml."""
    props = set(HOOKS_ADD.parameters["properties"])
    assert "path" not in props
    assert props == {"on", "message", "wake", "push_when", "name"}


def test_hooks_add_on_enum_is_isolated_from_module_hook_points_list() -> None:
    """Tier 2: HOOKS_ADD's rendered ``on`` enum is decoupled from the module-level
    ``_HOOK_POINTS`` list — mutating that list must NOT leak into the rendered
    schema (#2898 shared-mutable-state × test-order isolation).

    ``render_for_router`` only shallow-copies ``parameters``, so a by-reference
    embed of ``_HOOK_POINTS`` would alias the module list into every render;
    one stray mutation would then pollute every later ``hooks_add`` render (the
    exact flake class). The schema embeds a defensive copy, so this cannot
    happen. Falsification: append to the module list, render, assert the enum
    is unchanged (the append is undone in ``finally`` so this test leaves no
    global-state residue of its own)."""
    import reyn.tools.hooks as hooks_mod

    def _rendered_on_enum() -> list[str]:
        return hooks_mod.HOOKS_ADD.render_for_router()[
            "function"]["parameters"]["properties"]["on"]["enum"]

    expected = list(hooks_mod._HOOK_POINTS)
    assert _rendered_on_enum() == expected, (
        "precondition: the rendered enum should match the module hook points"
    )

    _POLLUTANT = "__2898_pollutant_hook_point__"
    hooks_mod._HOOK_POINTS.append(_POLLUTANT)
    try:
        assert _POLLUTANT not in _rendered_on_enum(), (
            "mutating the module-level _HOOK_POINTS list leaked into HOOKS_ADD's "
            "rendered `on` enum — the schema must embed a defensive copy so a "
            "shared-mutable mutation cannot pollute a later render (#2898)"
        )
        assert _rendered_on_enum() == expected
    finally:
        hooks_mod._HOOK_POINTS.remove(_POLLUTANT)


@pytest.mark.asyncio
async def test_hooks_add_writes_in_set_only_not_reyn_yaml(tmp_path: Path) -> None:
    """Tier 2: the op writes .reyn/hooks.yaml (IN-set) + leaves reyn.yaml (OUT-set)
    untouched — the write-gate falsify (it CANNOT touch the OUT-set)."""
    (tmp_path / "reyn.yaml").write_text("permissions:\n  shell: deny\n", encoding="utf-8")
    out_before = (tmp_path / "reyn.yaml").read_text()
    set_active_hot_reloader(HotReloader(project_root=tmp_path, events=EventLog()))

    result = await _handle_hooks_add({"on": "turn_end", "message": "hi"}, _ctx(tmp_path))

    assert result["status"] == "ok"
    assert (tmp_path / ".reyn" / "config" / "hooks.yaml").exists()           # IN-set written
    assert (tmp_path / "reyn.yaml").read_text() == out_before     # OUT-set untouched


@pytest.mark.asyncio
async def test_hooks_add_schedules_reload(tmp_path: Path) -> None:
    """Tier 2: the op schedules a reload via the active HotReloader."""
    hr = HotReloader(project_root=tmp_path, events=EventLog())
    set_active_hot_reloader(hr)
    result = await _handle_hooks_add({"on": "turn_end", "message": "hi"}, _ctx(tmp_path))
    assert result["reload_scheduled"] is True
    assert hr.pending is True


@pytest.mark.asyncio
async def test_per_session_route_reloads_caller_not_the_global(tmp_path: Path) -> None:
    """Tier 2: multi-session correctness (#2073 S3) — hooks_add reloads the CALLING
    session's reloader (ctx.hot_reloader), NOT the process-wide last-registered one.
    The reloader is per-session (unlike cron's single scheduler), so in multi-agent
    agent A's self-added hook must take effect on A, not on the last-constructed B."""
    caller = HotReloader(project_root=tmp_path, events=EventLog())   # the calling session (A)
    other = HotReloader(project_root=tmp_path, events=EventLog())    # the last-registered (B)
    set_active_hot_reloader(other)  # B is the process-wide active reloader

    ctx = ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(root=tmp_path), caller_kind="router",
        hot_reloader=caller,  # the calling session threads its own reloader
    )
    await _handle_hooks_add({"on": "turn_end", "message": "a-hook"}, ctx)

    assert caller.pending is True    # the caller (A) was reloaded
    assert other.pending is False    # the global (B) was NOT (pre-fix bug)


@pytest.mark.asyncio
async def test_hooks_add_rejects_invalid_hook_no_write(tmp_path: Path) -> None:
    """Tier 2: write-time validate — a bad hook (invalid lifecycle point) returns an
    error and writes nothing."""
    result = await _handle_hooks_add({"on": "not_a_point", "message": "hi"}, _ctx(tmp_path))
    assert result["status"] == "error"
    assert not (tmp_path / ".reyn" / "config" / "hooks.yaml").exists()


@pytest.mark.asyncio
async def test_hooks_add_dedups_idempotent(tmp_path: Path) -> None:
    """Tier 2: re-adding the same hook is idempotent (no duplicate accumulation)."""
    set_active_hot_reloader(HotReloader(project_root=tmp_path, events=EventLog()))
    await _handle_hooks_add({"on": "turn_end", "message": "hi"}, _ctx(tmp_path))
    again = await _handle_hooks_add({"on": "turn_end", "message": "hi"}, _ctx(tmp_path))
    assert again["added"] is False
    import yaml
    data = yaml.safe_load((tmp_path / ".reyn" / "config" / "hooks.yaml").read_text())
    assert [h.get("on", h.get(True)) for h in data["hooks"]] == ["turn_end"]  # single entry


def test_hooks_add_registered_as_a_tool() -> None:
    """Tier 2: hooks_add is registered in the tool catalog (router-callable)."""
    from reyn.tools import get_default_registry
    assert "hooks_add" in get_default_registry()


# ── crown-jewel E2E: agent adds a hook → reload → the hook fires ────────────


@pytest.mark.asyncio
async def test_e2e_agent_adds_hook_applies_at_boundary(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the full self-reload path — the agent calls hooks_add (writes
    .reyn/hooks.yaml + request_reload on the session's active reloader); at the turn
    boundary apply_pending runs the S2b hooks seam (re-combine startup ∪ runtime +
    replace_registry); the newly-added hook then FIRES (observed via the inbox)."""
    monkeypatch.chdir(tmp_path)
    # Session construction registers itself as the active reloader (#2073 S3).
    session = make_session(
        agent_name="s3-agent",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )

    result = await _handle_hooks_add(
        {"on": "turn_end", "message": "self-go", "wake": True}, _ctx(tmp_path),
    )
    assert result["reload_scheduled"] is True

    # turn boundary: the scheduled reload applies the hooks seam.
    await session._hot_reloader.apply_pending()

    # the newly-added runtime hook now fires.
    await session._hook_dispatcher.dispatch("turn_end", {})
    texts = set()
    while not session.inbox.empty():
        _kind, payload = session.inbox.get_nowait()
        texts.add(payload.get("text"))
    assert "self-go" in texts
