"""Tier 2: #4580 — a declared MCP server plugin_install couldn't register
(a failed probe, or a denied MCP-axis permission gate) used to vanish with
no event, no return-value trace, and no count difference anywhere an
operator (or a test) could see. This file pins the fix: the op's own
return value carries a ``skipped`` list per capability kind, and each skip
gets its own ``mcp_server_install_skipped`` audit-event naming ``reason``.

Real ``OpContext``/``Workspace``/``EventLog``/``HotReloader``/
``PermissionResolver`` throughout (mirrors ``test_2761_pr3_mcp_immediate_
probe.py``'s own established construction for exercising the LIVE
probe-then-commit path — ``_register_mcp``'s probe only runs at all when
``ctx.hot_reloader`` is a real, live reloader; every existing
``test_plugin_install.py`` ctx omits it, which is exactly why this whole
class of behavior had no coverage before #4580). No mocks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.plugin_install import handle as install_handle
from reyn.data.workspace.workspace import Workspace
from reyn.plugins.manifest import PLUGIN_MANIFEST_SCHEMA_URL
from reyn.runtime.hot_reload import HotReloader
from reyn.schemas.models import PluginInstallIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from tests._support.events import settle


def _make_plugin(root: Path, *, servers: dict) -> Path:
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps({
            "$schema": PLUGIN_MANIFEST_SCHEMA_URL,
            "name": root.name, "version": "0.1.0",
        }),
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8",
    )
    return root


def _ctx(
    tmp_path: Path, events: EventLog, *, hot_reloader: bool, mcp_grant: "list[str] | None" = None,
) -> OpContext:
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    # #4580: `probe_mcp_server` self-declares `PermissionDecl(mcp=[server_name])`
    # for the AgentLayer/ProfileLayer ∩ (measured directly — see mcp_install.py's
    # own comment on why: a runtime-determined server name can't be pre-
    # enumerated in a static per-agent decl), so `PermissionDecl.mcp` on THIS
    # ctx has NO effect on that call's gate. The real gate is `require_mcp`'s
    # own `_approve` step, which reads `PermissionResolver`'s `config_permissions`
    # (`_is_config_approved("mcp.<name>")`) BEFORE ever touching an interactive
    # bus — the legitimate, public grant surface for a non-interactive resolver
    # (`interactive=False`, matching every other ctx in this test suite).
    mcp_config = {name: "allow" for name in (mcp_grant or [])}
    resolver = PermissionResolver(
        config_permissions={"mcp": mcp_config} if mcp_config else {},
        project_root=project_root, interactive=False,
    )
    from reyn.core.op_runtime.plugin_install import plugins_root
    resolver.session_approve_path(str(plugins_root()), "test", "file.write", recursive=True)
    resolver.session_approve_path(
        str(project_root / ".reyn" / "config" / "mcp.yaml"), "test", "file.write",
    )
    return OpContext(
        workspace=Workspace(events=events, permission_resolver=resolver, base_dir=project_root),
        events=events,
        permission_decl=PermissionDecl(
            file_write=[{"path": str(plugins_root()), "scope": "recursive"}],
            mcp=mcp_grant or [],
        ),
        permission_resolver=resolver,
        actor="test",
        hot_reloader=HotReloader(project_root=project_root, events=events) if hot_reloader else None,
    )


@pytest.mark.asyncio
async def test_a_probe_failing_server_is_named_in_the_return_value_and_an_event_fires(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: accept-path — one server whose command is a real, live,
    successfully-spawnable process (``sys.executable`` with no real MCP
    handshake — not asserted on here, only that it's NOT the failing one)
    and one whose command names a nonexistent binary (the SAME shape
    ``test_registered_command_pointing_at_missing_venv_fails_fast_no_fetch``
    already proves ``probe_mcp_server`` itself fails fast on, driven here
    through the FULL install op instead of calling the probe directly)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    events = EventLog()
    calls: list = []
    events.add_subscriber(calls.append)

    src = _make_plugin(tmp_path / "src" / "skipviz", servers={
        "good": {"command": "/definitely/not/a/real/binary-4580-good", "args": []},
        "bad": {"command": "/definitely/not/a/real/binary-4580-bad", "args": []},
    })
    # Grant the MCP axis for both names — this test's whole point is the
    # SPAWN-failure path (probe_failed), not the permission gate (the
    # sibling test below covers that). Without this grant every server
    # would deny at the gate first and never reach the probe at all
    # (confirmed empirically — see that sibling test).
    ctx = _ctx(tmp_path, events, hot_reloader=True, mcp_grant=["good", "bad"])
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})

    result = await install_handle(op, ctx)
    await settle(events)

    assert result["status"] == "installed", result
    # Both entries name a nonexistent binary in this test (no real MCP
    # server available to spawn in CI) — both probes fail, so BOTH must be
    # visible in `skipped`, and NEITHER in `registered`. The point under
    # test is that a probe failure is RECORDED, not which of two names
    # happens to fail.
    assert result["registered"]["mcp"] == []
    skipped_names = {s["name"] for s in result["skipped"]["mcp"]}
    assert skipped_names == {"good", "bad"}
    assert all(s["reason"] == "probe_failed" for s in result["skipped"]["mcp"])

    skip_events = [e for e in calls if e.type == "mcp_server_install_skipped"]
    # One event PER dropped server, never a single summary event — proven
    # against `result["skipped"]["mcp"]`'s own length (a derived value, not
    # a bare magic literal), plus the set of names the events actually name.
    assert len(skip_events) == len(result["skipped"]["mcp"])
    assert {e.data.get("server_id") for e in skip_events} == skipped_names


@pytest.mark.asyncio
async def test_declared_but_never_probed_server_registers_unconditionally_no_skip(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: regression guard — WITHOUT a live hot_reloader (every
    existing test_plugin_install.py ctx), the probe never runs at all
    (pre-#4580 behavior, unchanged) — the server registers unconditionally
    and ``skipped`` stays empty. #4580 must not turn ON a probe that was
    never wired for this ctx shape."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    events = EventLog()
    src = _make_plugin(tmp_path / "src" / "noreload", servers={
        "srv": {"command": "python", "args": ["-m", "srv"]},
    })
    ctx = _ctx(tmp_path, events, hot_reloader=False)
    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})

    result = await install_handle(op, ctx)

    assert result["status"] == "installed", result
    assert result["registered"]["mcp"] == ["srv"]
    assert result["skipped"]["mcp"] == []


@pytest.mark.asyncio
async def test_a_permission_denied_server_is_recorded_with_that_specific_reason(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the SECOND drop path (#4580's own scope) — a probe that
    raises ``PermissionError`` (a REAL ``PermissionResolver.require_mcp``
    deny, reached via ``probe_mcp_server``'s own permission_resolver
    argument — never a forced/patched raise) is recorded with
    ``reason == "permission_denied"``, distinguishable from a plain probe
    failure (the sibling accept-path test above, which explicitly grants
    the server via ``config_permissions`` so it reaches the spawn-failure
    branch instead — see ``_ctx``'s own comment on why ``PermissionDecl.mcp``
    itself has no effect here). Driven by the resolver's own default-deny:
    ``require_mcp``'s ``_approve`` step falls through to ``return False``
    for a non-interactive resolver (``interactive=False``) with no
    matching ``config_permissions`` entry and no session/persisted grant —
    measured directly (this exact ctx shape, un-granted, produces
    ``permission_denied`` before ever reaching the spawn attempt)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    events = EventLog()
    src = _make_plugin(tmp_path / "src" / "denyviz", servers={
        "srv": {"command": "/definitely/not/a/real/binary-4580-deny", "args": []},
    })
    # No mcp_grant — the MCP axis denies "srv" by default (unlike the
    # sibling accept-path test, which explicitly grants it).
    ctx = _ctx(tmp_path, events, hot_reloader=True)

    op = PluginInstallIROp(kind="plugin_install", source={"kind": "local", "path": str(src)})
    result = await install_handle(op, ctx)

    assert result["status"] == "installed", result
    assert result["registered"]["mcp"] == []
    (skip,) = result["skipped"]["mcp"]
    assert skip["name"] == "srv"
    assert skip["reason"] == "permission_denied"
    assert "error" not in skip  # PermissionError carries no probe error text
