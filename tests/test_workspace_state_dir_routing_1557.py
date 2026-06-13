"""Tier 2: OS invariant — shadow git-dir routes under --state-dir (#1557 gap-#1).

#1544 follow-up coherence fix: the shadow-git workspace-version store's git-dir
is part of the persisted OS-state set. When ``--state-dir`` (workspace_state_dir)
is provided, the shadow git-dir lives under it (alongside events/artifacts) — one
persistence switch — instead of always at ``project_root/.reyn``. Additive: with
no state-dir, the default location is unchanged.

Behavioral assertion (no private-state): ``capture`` creates the git-dir on disk
at the configured location, so the filesystem proves where it was routed. Real
AgentRegistry + WorkspaceVersionStore + git (no mocks).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reyn.chat.registry import AgentRegistry
from reyn.events.state_log import StateLog

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for the workspace substrate",
)


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


@pytest.mark.asyncio
async def test_shadow_git_dir_routes_under_state_dir_when_set(tmp_path):
    """Tier 2: with workspace_state_dir set, the shadow git-dir lives under it."""
    proj = tmp_path / "proj"
    proj.mkdir()
    state = tmp_path / "host-state"
    state.mkdir()
    state_log = StateLog(proj / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=proj,
        session_factory=_no_factory,
        state_log=state_log,
        workspace_state_dir=state,
    )

    (proj / "code.py").write_text("v1", encoding="utf-8")
    await reg.workspace_store.capture(1)

    assert (state / "workspace-shadow.git" / "HEAD").exists()        # routed under --state-dir
    assert not (proj / ".reyn" / "workspace-shadow.git").exists()    # NOT at the default


@pytest.mark.asyncio
async def test_shadow_git_dir_defaults_under_project_root(tmp_path):
    """Tier 2: with no workspace_state_dir, the default git-dir location is unchanged."""
    proj = tmp_path / "proj"
    proj.mkdir()
    state_log = StateLog(proj / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=proj,
        session_factory=_no_factory,
        state_log=state_log,
    )  # no workspace_state_dir

    (proj / "code.py").write_text("v1", encoding="utf-8")
    await reg.workspace_store.capture(1)

    assert (proj / ".reyn" / "workspace-shadow.git" / "HEAD").exists()   # default unchanged
