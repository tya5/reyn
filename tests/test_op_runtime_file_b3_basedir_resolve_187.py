"""Tier 2: #187 B3 — the file-op handler resolves the path against the workspace
base_dir BEFORE the permission gate (in-container write-lands).

B3 root cause (deterministic, primary-evidence repro): under a container backend
(``base_dir=/testbed``) the agent's relative repo write (``astropy/io/...``) was
passed RAW to ``require_file_write``; the gate's SandboxLayer resolved it with
``Path(path).resolve()`` against the HOST process cwd — not /testbed — so it fell
outside the sandbox ``write_paths`` cap (``[/testbed]``) and was DENIED, even
though ``Workspace.write_file`` resolves the same path against /testbed and lands
it there. A ``file.write=allow`` config permission (#3924: this used to be
what ``--grant-file-write`` injected, now written directly in reyn.yaml)
already bypasses the AgentLayer zone, so the SandboxLayer ∩ on the
relative-vs-cwd mismatch was the real denier (NOT the project_root zone
anchor).

The fix resolves the path against ``ctx.workspace.base_dir`` before the gate, so
the permission check sees the SAME absolute target the write/read will hit. These
tests pin the round-trip (granted AND lands) and read/write symmetry — with real
instances (no mocks).

#3901 PR-B ③ retired FILE_READ/FILE_WRITE from SandboxLayer's permission-∩
projection (an operator cannot know a sandbox's path floor, so it is no
longer treated as permission) — this module's original sandbox-cap
falsification test relied on that intersection and was removed accordingly
(a comment marks where it lived and why); the round-trip / write-lands
assertions below are unaffected, since they never depended on a sandbox
denial to pass.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


def _ctx(tmp_path: Path, base_dir: Path, *, write_cap: Path) -> OpContext:
    """An OpContext mirroring the run-once-in-container scoping: workspace rooted
    on a non-cwd base_dir, config file.write/read=allow (#3924: an operator's
    reyn.yaml permissions declaration, not a CLI flag anymore),
    project_root on the HOST, and a sandbox capping write paths to the
    container repo. #3901 PR-B ③ retired FILE_READ from SandboxLayer's
    permission-∩ projection, so this ctx no longer carries a read cap — see
    test_b3_relative_read_resolves_against_base_dir's own docstring."""
    events = EventLog()
    ws = Workspace(events, base_dir=base_dir, state_dir=tmp_path / "state")
    resolver = PermissionResolver(
        config_permissions={"file.write": "allow", "file.read": "allow"},
        project_root=tmp_path / "host",  # host anchor ≠ base_dir (the B3 condition)
        interactive=False,
    )
    return OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver,
        default_sandbox_policy={
            "write_paths": [str(write_cap)],
            "network": False,
        },
    )


@pytest.mark.asyncio
async def test_b3_relative_write_resolves_against_base_dir_and_lands(tmp_path):
    """Tier 2: a relative repo write under a non-cwd base_dir is GRANTED and lands (#187 B3).

    Pre-fix the raw relative path resolved against host cwd, landing outside
    the workspace base_dir. Resolving against base_dir first makes the write
    target /testbed/astropy/... and land there (round-trip / write-lands, the
    #1410 lesson) — independent of whether a sandbox cap is in play (#3901
    PR-B ③ retired FILE_WRITE from SandboxLayer's permission-∩, so a
    resolution bug here can no longer be caught by a sandbox denial; the
    write-lands assertion is what still catches it)."""
    testbed = tmp_path / "testbed"
    (testbed / "astropy" / "io").mkdir(parents=True)
    ctx = _ctx(tmp_path, testbed, write_cap=testbed)

    op = FileIROp(kind="file", op="write", path="astropy/io/html.py", content="X = 1\n")
    res = await handle(op, ctx)

    assert res["status"] == "ok"
    # write-lands: the file is under the workspace base_dir, not the host cwd.
    assert (testbed / "astropy" / "io" / "html.py").read_text() == "X = 1\n"


# #3901 PR-B ③ retired FILE_WRITE from SandboxLayer's permission-∩ projection
# (an operator cannot know a sandbox's path floor, so it is no longer treated
# as permission — lead-coder confirmed this stays retired, #3901 thread,
# distinct from the NETWORK_HOST ruling). The falsification this file used to
# carry here (`test_b3_sandbox_write_cap_still_load_bearing`: an out-of-cap
# sandbox write_paths DENIES the write) pinned a guarantee SandboxLayer no
# longer provides — this ctx's ``config_permissions={"file.write": "allow"}``
# means AgentLayer would not deny it either, so there is no live mechanism
# left to falsify against. Deleted rather than left to rot RED for a reason
# unrelated to what it claimed to test (six-questions: "should this test
# exist" resolves to no, not "repair it").


@pytest.mark.asyncio
async def test_b3_relative_read_resolves_against_base_dir(tmp_path):
    """Tier 2: read/write symmetry — a relative read under a non-cwd base_dir
    is GRANTED and reads the file actually under base_dir (the read gate also
    resolves against base_dir, not host cwd)."""
    testbed = tmp_path / "testbed"
    (testbed / "astropy").mkdir(parents=True)
    (testbed / "astropy" / "io.py").write_text("data = 2\n")
    ctx = _ctx(tmp_path, testbed, write_cap=testbed)

    op = FileIROp(kind="file", op="read", path="astropy/io.py")
    res = await handle(op, ctx)

    assert res.get("status") != "denied"
    # the content read is the file under base_dir (resolved correctly).
    assert "data = 2" in str(res)
