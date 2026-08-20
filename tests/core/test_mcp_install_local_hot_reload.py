"""Tier 2: mcp_install_local schedules a hot-reload after writing config.

Before this fix, _handle_mcp_install_local bypassed mcp_install_handle and
wrote .reyn/mcp.yaml directly without calling request_reload. An installed
local server never appeared in the same session's list_mcp_servers (required
a restart). The other verbs (mcp_install_registry / mcp_install_package)
route through mcp_install_handle which calls request_reload — local is now
aligned.

Falsify: removing the request_reload call makes test_local_install_schedules_reload
fail (pending stays False) while no-reloader and entry-shape tests remain green.

#3636: the hand-rolled ``_FakeReloader`` stand-in this file used to construct
was replaced with a REAL ``HotReloader`` (cheaply constructible — a
``project_root`` + an ``EventLog``) after it silently drifted out of sync
with ``HotReloader.request_reload``'s signature (the #3636 fix added an
optional ``detail`` kwarg; the hand-rolled fake didn't accept it and raised
``TypeError`` — exactly the signature-drift class a faked callable is meant
to catch, per ``docs/deep-dives/contributing/testing.md`` § Mock vs Fake).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from reyn.core.events.events import EventLog
from reyn.runtime.hot_reload import HotReloader
from reyn.tools.types import ToolContext
from tests._support.events import collect_events, settle


def _ctx(project_root: Path) -> ToolContext:
    """The REAL ToolContext (a plain dataclass — a real instance, not a stand-in).

    This used to be a hand-rolled ``_FakeCtx`` carrying a ``_FakePermissionResolver``
    whose ``require_file_write`` signature was frozen at the call shape of the day. Both
    are gone: the fake resolver made the permission gate look exercised here while the
    handler read its resolver off ``router_state`` (a field the real ``RouterCallerState``
    never declared), so nothing was gated in production. ``permission_resolver=None``
    keeps these tests on their own invariant — the hot-reload dispatch — and the gate is
    covered against a REAL ``PermissionResolver`` in
    test_3037_mcp_install_local_recovery_core_gate.py.
    """
    return ToolContext(
        events=None,
        permission_resolver=None,
        workspace=SimpleNamespace(root=str(project_root)),
        caller_kind="router",
        router_state=None,
    )


def _run_install(
    reloader: "HotReloader | None",
    project_root: Path,
    name: str = "local-test",
    command: str = "python",
    args: list[str] | None = None,
) -> dict:
    """Run _handle_mcp_install_local with real disk I/O against project_root."""
    import reyn.runtime.hot_reload as hr_mod
    from reyn.tools.mcp_verbs import _handle_mcp_install_local

    orig = hr_mod._active_hot_reloader
    try:
        hr_mod._active_hot_reloader = reloader
        return asyncio.run(
            _handle_mcp_install_local(
                {"name": name, "command": command, "args": args or ["/tmp/server.py"]},
                _ctx(project_root),
            )
        )
    finally:
        hr_mod._active_hot_reloader = orig


def test_local_install_schedules_reload(tmp_path: Path) -> None:
    """Tier 2: mcp_install_local calls request_reload — installed server visible next turn.

    Drives the real HotReloader through to ``apply_pending`` (the turn-boundary
    apply) and asserts the emitted ``config_reloaded`` P6 event carries
    ``source="mcp_install_local"`` — confirming request_reload was scheduled
    with the right source via the PUBLIC events log, not a private-state read.
    """
    events = EventLog()
    collected = collect_events(events)
    reloader = HotReloader(project_root=tmp_path, events=events)
    result = _run_install(reloader, tmp_path)
    assert result["status"] == "ok", f"install failed: {result}"
    assert reloader.pending is True, "request_reload must fire — server won't appear without reload"

    async def _apply_and_settle() -> None:
        await reloader.apply_pending()
        await settle(events)

    asyncio.run(_apply_and_settle())
    # Tuple-unpack: raises ValueError if there isn't EXACTLY one — a
    # behavioural assertion (one reload, not a repeat), not a length pin.
    (reload_event,) = [e for e in collected if e.type == "config_reloaded"]
    assert reload_event.data["source"] == "mcp_install_local"


def test_local_install_no_reload_when_no_active_reloader(tmp_path: Path) -> None:
    """Tier 2: no active reloader (CLI / subprocess) → success, not a crash."""
    result = _run_install(reloader=None, project_root=tmp_path)
    assert result["status"] == "ok"


def test_local_install_returns_entry_shape(tmp_path: Path) -> None:
    """Tier 2: result carries the registered entry so callers can confirm what was written."""
    reloader = HotReloader(project_root=tmp_path, events=EventLog())
    result = _run_install(reloader, tmp_path)
    data = result["data"]
    assert data["kind"] == "mcp_install_local"
    assert data["name"] == "local-test"
    assert "entry" in data
    assert data["entry"]["command"] == "python"


def test_local_install_writes_config_to_disk(tmp_path: Path) -> None:
    """Tier 2: the server entry actually lands in .reyn/config/mcp.yaml."""
    from reyn.core.op_runtime.mcp_install import _read_yaml_config, _scope_to_path

    reloader = HotReloader(project_root=tmp_path, events=EventLog())
    _run_install(reloader, tmp_path, name="my-server", command="node")
    config_path = _scope_to_path("local", tmp_path)
    data = _read_yaml_config(config_path)
    servers = data.get("mcp", {}).get("servers", {})
    assert "my-server" in servers, f"server not in config: {servers}"
    assert servers["my-server"]["command"] == "node"
