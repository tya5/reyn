"""Tier 2: OS invariant — #2248 PR-B config-path consistency (A2↔B round-trip, REAL producer).

PR-B relocates the recovery-core config registries from ``.reyn/<name>.yaml`` to
``.reyn/config/<name>.yaml``. The hazard this guards is split-brain: a config op whose LIVE
write moves to ``config/`` but whose ``config_changed`` WAL key (or the registry's rewind
write-back path) does NOT — live writes at the new path, recovery at the old.

This is the same shape as ``test_2248_pr_a2_config_emission`` but pinned to the NEW path: a REAL
``mcp_drop_server`` op writes to ``.reyn/config/mcp.yaml``, emits ``config_changed`` keyed
``"config/mcp.yaml"`` (``reyn_relative_path`` returns the path BELOW ``.reyn/``), and
``AgentRegistry._reconcile_config_as_of_cut`` reconstructs to the NEW path on rewind — proving
the write-path, the WAL key, and the rewind write-back all agree (no split-brain).

Real PermissionResolver + StateLog + AgentRegistry + on-disk yaml (no mocks).
"""
from __future__ import annotations

import pytest
import yaml

from reyn.core.events.config_recovery import record_config_change, reyn_relative_path
from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime.context import OpContext
from reyn.runtime.registry import AgentRegistry
from reyn.schemas.models import MCPDropServerIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


class _StubWorkspace:
    def __init__(self, base_dir) -> None:
        self.base_dir = base_dir


class _Events:
    subscribers: list = []

    def emit(self, *_a, **_k) -> None:
        pass


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def test_reyn_relative_path_follows_the_config_move():
    """Tier 2: ``reyn_relative_path`` returns the ``.reyn``-relative key for the NEW
    config-subdir location — ``…/.reyn/config/mcp.yaml`` → ``config/mcp.yaml`` — so the WAL
    key tracks the moved write-path automatically. RED if the move broke the relative key."""
    assert reyn_relative_path("/tmp/proj/.reyn/config/mcp.yaml") == "config/mcp.yaml"
    assert (
        reyn_relative_path("/tmp/proj/.reyn/config/index/sources.yaml")
        == "config/index/sources.yaml"
    )


@pytest.mark.asyncio
async def test_real_mcp_drop_writes_config_subdir_emits_keyed_path_and_rewind_restores(
    tmp_path,
):
    """Tier 2: a REAL mcp_drop op writes ``.reyn/config/mcp.yaml``, emits config_changed keyed
    ``config/mcp.yaml``, and a rewind reconstructs to the SAME new path. RED on split-brain
    (live write moved but WAL key / rewind write-back still pointed at the old ``.reyn/mcp.yaml``)."""
    from reyn.core.op_runtime.mcp_drop_server import handle as drop_handle

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    # The NEW canonical location — the op auto-detects "dynamic" → .reyn/config/mcp.yaml.
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
        state_log=state_log,
    )
    op = MCPDropServerIROp(
        kind="mcp_drop_server", server="brave", scope=None, clear_secrets=False,
    )
    result = await drop_handle(op=op, ctx=ctx, caller="control_ir")
    assert result["status"] == "ok"

    # 1) the LIVE write landed at the NEW config-subdir path (not the old top-level one).
    assert mcp_path.is_file(), "op wrote to .reyn/config/mcp.yaml"
    assert not (tmp_path / ".reyn" / "mcp.yaml").exists(), "no split-brain write at old path"

    # 2) the REAL op emitted config_changed keyed by the NEW .reyn-relative path.
    [ev] = [e for e in state_log.iter_from(cut + 1) if e.get("kind") == "config_changed"]
    assert ev["path"] == "config/mcp.yaml"
    assert set(ev["content"]["mcp"]["servers"]) == {"filesystem"}

    # 3) rewind reconstructs to the SAME new path from the WAL truth (no old-path resurrection).
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )
    reg._reconcile_config_as_of_cut(cut)
    restored = yaml.safe_load(mcp_path.read_text(encoding="utf-8"))["mcp"]["servers"]
    assert set(restored) == {"filesystem", "brave"}, "rewind reconstructs at .reyn/config/mcp.yaml"
    assert not (tmp_path / ".reyn" / "mcp.yaml").exists(), "rewind did not write the old path"
