"""Tier 2: #1442 — mcp install targets the resolved project root, not cwd.

Three independent defects let `mcp install` write to (or crash on) the wrong
workspace:

- **A (CLI source):** no `--project` flag + cwd-only resolution → silent wrong
  target. `_resolve_install_project_root` adds `--project` + fail-loud.
- **B (handler attribute):** the handler read `ctx.workspace.root`, but the real
  Workspace exposes `.base_dir` → it silently fell back to cwd.
  `_resolve_write_root` reads the canonical `base_dir` (with `.root`/cwd
  fallbacks).
- **C (agent path crash):** the agent verbs built `OpContext` without the
  required `workspace` → TypeError. They now thread `ctx.workspace`.

Real Workspace / real verbs; the only injected double is a recording coroutine
for the network-doing install handler (the sanctioned op-seam) — no mocks.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pytest

from reyn.cli.commands.mcp import _resolve_install_project_root, register
from reyn.events.events import EventLog
from reyn.op_runtime.mcp_install import _resolve_write_root
from reyn.tools.mcp_verbs import (
    _handle_mcp_install_package,
    _handle_mcp_install_registry,
)
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.workspace.workspace import Workspace

# ── Layer A: --project + resolve-once + fail-loud ───────────────────────────


def test_resolve_project_root_from_explicit_project(tmp_path):
    """Tier 2: #1442 A — --project resolves to that root (not cwd)."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    assert _resolve_install_project_root(str(tmp_path)) == tmp_path.resolve()


def test_resolve_project_root_fails_loud_when_no_project(tmp_path, monkeypatch):
    """Tier 2: #1442 A — no --project and no reyn.yaml from cwd → loud exit, not a
    silent cwd write."""
    monkeypatch.chdir(tmp_path)  # no reyn.yaml here
    with pytest.raises(SystemExit):
        _resolve_install_project_root(None)


def test_install_parser_exposes_project_flag():
    """Tier 2: #1442 A — the install subcommand accepts --project (symmetric with
    serve/list/refresh)."""
    p = argparse.ArgumentParser(prog="reyn")
    register(p.add_subparsers(dest="cmd"))
    args = p.parse_args(["mcp", "install", "--project", "/tmp/x", "io.github.foo/bar"])
    assert args.project == "/tmp/x"


# ── Layer B: handler resolves the real Workspace base_dir ───────────────────


def test_resolve_write_root_uses_real_workspace_base_dir(tmp_path):
    """Tier 2: #1442 B — a REAL Workspace (base_dir != cwd) resolves to its
    base_dir, not cwd. This is the bug the `.root`-only check caused."""
    ws = Workspace(events=EventLog(), base_dir=tmp_path)
    assert _resolve_write_root(ws) == tmp_path.resolve()
    assert _resolve_write_root(ws) != Path.cwd()


def test_resolve_write_root_fallbacks(tmp_path):
    """Tier 2: #1442 B — base_dir preferred; legacy `.root` stub still works;
    None → cwd (safe last resort)."""
    assert _resolve_write_root(type("W", (), {"base_dir": str(tmp_path)})()) == tmp_path
    assert _resolve_write_root(type("W", (), {"root": str(tmp_path)})()) == tmp_path
    assert _resolve_write_root(None) == Path.cwd()


# ── Layer C: agent verbs thread the workspace (no TypeError crash) ───────────


def _ctx_with_workspace(ws) -> ToolContext:
    return ToolContext(
        events=EventLog(),
        permission_resolver=None,
        workspace=ws,
        caller_kind="router",
        router_state=RouterCallerState(),
    )


def _drive_verb(verb, args, ws, monkeypatch):
    """Drive a real mcp_verbs install verb with a recording handler seam; return
    the OpContext the verb built (the #1442-C crash site)."""
    captured: dict = {}

    async def _recording_handle(op, op_ctx, *, caller):
        captured["op_ctx"] = op_ctx
        return {"installed": True, "caller": caller}

    monkeypatch.setattr("reyn.op_runtime.mcp_install.handle", _recording_handle)
    result = asyncio.run(verb(args, _ctx_with_workspace(ws)))
    return result, captured


def test_agent_install_registry_threads_workspace_no_crash(tmp_path, monkeypatch):
    """Tier 2: #1442 C — agent-invoked registry install builds the OpContext WITH
    the caller's workspace (was a TypeError crash: required field omitted). The
    threaded workspace then resolves to its real base_dir, not cwd."""
    ws = Workspace(events=EventLog(), base_dir=tmp_path)
    result, captured = _drive_verb(
        _handle_mcp_install_registry, {"server_id": "io.github.foo/bar"}, ws, monkeypatch
    )
    assert result["status"] == "ok"  # no crash
    assert captured["op_ctx"].workspace is ws  # threaded (the C fix)
    assert _resolve_write_root(captured["op_ctx"].workspace) == tmp_path.resolve()


def test_agent_install_package_threads_workspace_no_crash(tmp_path, monkeypatch):
    """Tier 2: #1442 C — same for the package verb (the second OpContext site)."""
    ws = Workspace(events=EventLog(), base_dir=tmp_path)
    result, captured = _drive_verb(
        _handle_mcp_install_package,
        {"kind": "npm", "identifier": "bar-mcp"},
        ws,
        monkeypatch,
    )
    assert result["status"] == "ok"
    assert captured["op_ctx"].workspace is ws
