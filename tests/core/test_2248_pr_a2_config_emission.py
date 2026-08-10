"""Tier 2: OS invariant — #2259 config-recovery emission (end-to-end, REAL producer).

A real config op (``mcp_drop_server``) — handed a ``state_log`` via its OpContext (the
production wiring: session → RouterHostAdapter → ToolContext → adapter → OpContext) — records a
full-state config GENERATION carrying the FULL post-mutation mcp registry content after it
persists ``.reyn/config/mcp.yaml``. ``AgentRegistry._reconcile_config_as_of_cut`` then reverts
the registry on a rewind. This proves config-recovery with a REAL producer (not a test-only
``record_config_generation`` call): drop a server → its generation is recorded → rewind → it's back.

Real PermissionResolver + StateLog + AgentRegistry + on-disk yaml (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.config_recovery import record_config_generation
from reyn.core.events.snapshot_generations import rewind as _wal_rewind
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
async def test_real_mcp_drop_records_generation_and_rewind_restores(tmp_path):
    """Tier 2: a REAL mcp_drop op (state_log threaded into OpContext) records a config
    generation with the full post-drop mcp content; a rewind to before the drop reconstructs the
    dropped server from the generation. RED if the op didn't record (config invisible to recovery)
    or reconstruct trusted the on-disk post-drop yaml."""
    from reyn.core.op_runtime.mcp_drop_server import handle as drop_handle

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    mcp_path = tmp_path / ".reyn" / "config" / "mcp.yaml"
    mcp_path.parent.mkdir(parents=True, exist_ok=True)
    two_servers = {"mcp": {"servers": {
        "filesystem": {"command": "npx", "args": ["-y", "@mcp/fs"]},
        "brave": {"command": "uvx", "args": ["brave-mcp"]},
    }}}
    mcp_path.write_text(yaml.dump(two_servers), encoding="utf-8")
    # the prior install's generation (pre-drop state) — the seq we rewind to:
    await record_config_generation(state_log, str(mcp_path), two_servers)
    cut = state_log.current_seq
    # bump the WAL head so the drop's generation is filed at a DISTINCT seq > cut.
    await state_log.append("inbox_put", n=0)

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
        actor="test",
        intervention_bus=None,
        subscribers=[],
        state_log=state_log,  # the PR-A2 threading under test
    )
    # scope=None → auto-detect walks ("dynamic" = .reyn/mcp.yaml) first → the canonical
    # recovery-core location.
    op = MCPDropServerIROp(
        kind="mcp_drop_server", server="brave", scope=None, clear_secrets=False,
    )
    result = await drop_handle(op=op, ctx=ctx)
    assert result["status"] == "ok"

    # 1) the REAL op recorded a generation carrying the FULL post-drop registry state, AND the
    #    live yaml dropped brave. The generation reconstructs the post-drop state as-of-now.
    assert "brave" not in yaml.safe_load(mcp_path.read_text(encoding="utf-8"))["mcp"]["servers"]
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )
    reg._reconcile_config_as_of_cut(state_log.current_seq)
    post_drop = yaml.safe_load(mcp_path.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert set(post_drop) == {"filesystem"}, "the op's generation reconstructs the post-drop state"

    # 2) rewind to before the drop → reconstruct restores brave from the generation truth.
    # Add a rewind record targeting cut so the post-drop generation lands in the abandoned
    # interval (cut, R) and is correctly excluded by is_active_seq. Production invariant:
    # _reconcile_config_as_of_cut is always called from _materialize_rewind, which has an
    # active rewind record by construction.
    await _wal_rewind(state_log, target_n=cut)
    reg._reconcile_config_as_of_cut(cut)
    restored = yaml.safe_load(mcp_path.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert set(restored) == {"filesystem", "brave"}, "rewind reconstructs the dropped server"
