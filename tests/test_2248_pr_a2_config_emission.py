"""Tier 2: OS invariant — #2248 PR-A2 config-recovery emission (end-to-end, REAL producer).

A real config op (``mcp_drop_server``) — handed a ``state_log`` via its OpContext (the
production wiring: session → RouterHostAdapter → ToolContext → adapter → OpContext) — emits a
``config_changed`` WAL event carrying the FULL post-mutation mcp registry content after it
persists ``.reyn/mcp.yaml``. ``AgentRegistry._reconcile_config_as_of_cut`` then reverts the
registry on a rewind. This proves config-recovery with a REAL producer (not a test-only
``record_config_change`` call): drop a server → it's in the WAL → rewind → it's back.

Real PermissionResolver + StateLog + AgentRegistry + on-disk yaml (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.config_recovery import record_config_change
from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.runtime.registry import AgentRegistry
from reyn.schemas.models import MCPDropServerIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


class _StubWorkspace:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir


class _Events:
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


@pytest.mark.asyncio
async def test_real_mcp_drop_emits_config_changed_and_rewind_restores(tmp_path):
    """Tier 2: a REAL mcp_drop op (state_log threaded into OpContext) emits config_changed
    with the full post-drop mcp content; a rewind to before the drop reconstructs the dropped
    server from the WAL. RED if the op didn't emit (config invisible to replay) or reconstruct
    trusted the on-disk post-drop yaml."""
    from reyn.core.op_runtime.mcp_drop_server import handle as drop_handle

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    mcp_path = tmp_path / ".reyn" / "config" / "mcp.yaml"
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    two_servers = {"mcp": {"servers": {
        "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
        "brave": {"command": "uvx", "args": ["brave-mcp"]},
    }}}
    mcp_path.write_text(yaml.dump(two_servers), encoding="utf-8")
    # the prior install's config_changed (pre-drop state) — the seq we rewind to:
    await record_config_change(state_log, "config/mcp.yaml", two_servers)
    cut = state_log.current_seq

    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    canonical = str(mcp_path)
    resolver.session_approve_path(canonical, "test", "file.write")
    ctx = OpContext(
        workspace=_StubWorkspace(base_dir=tmp_path),
        events=_Events(),
        permission_decl=PermissionDecl(
            file_write=[{"path": canonical, "scope": "just_path"}],
        ),
        permission_resolver=resolver,
        skill_name="test",
        intervention_bus=None,
        subscribers=[],
        state_log=state_log,  # the PR-A2 threading under test
    )
    # scope=None → auto-detect walks ("dynamic" = .reyn/mcp.yaml) first → the canonical
    # recovery-core location.
    op = MCPDropServerIROp(
        kind="mcp_drop_server", server="brave", scope=None, clear_secrets=False,
    )
    result = await drop_handle(op=op, ctx=ctx, caller="control_ir")
    assert result["status"] == "ok"

    # 1) the REAL op emitted config_changed carrying the FULL post-drop registry state.
    [ev] = [e for e in state_log.iter_from(cut + 1) if e.get("kind") == "config_changed"]
    assert ev["path"] == "config/mcp.yaml"
    assert set(ev["content"]["mcp"]["servers"]) == {"filesystem"}
    assert "brave" not in yaml.safe_load(mcp_path.read_text(encoding="utf-8"))["mcp"]["servers"]

    # 2) rewind to before the drop → reconstruct restores brave from the WAL truth.
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )
    reg._reconcile_config_as_of_cut(cut)
    restored = yaml.safe_load(mcp_path.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert set(restored) == {"filesystem", "brave"}, "rewind reconstructs the dropped server"
