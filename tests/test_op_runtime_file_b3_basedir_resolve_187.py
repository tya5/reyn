"""Tier 2: #187 B3 — the file-op handler resolves the path against the workspace
base_dir BEFORE the permission gate (in-container write-lands).

B3 root cause (deterministic, primary-evidence repro): under a container backend
(``base_dir=/testbed``) the agent's relative repo write (``astropy/io/...``) was
passed RAW to ``require_file_write``; the gate's SandboxLayer resolved it with
``Path(path).resolve()`` against the HOST process cwd — not /testbed — so it fell
outside the sandbox ``write_paths`` cap (``[/testbed]``) and was DENIED, even
though ``Workspace.write_file`` resolves the same path against /testbed and lands
it there. ``--grant-file-write`` (config file.write=allow) already bypasses the
AgentLayer zone, so the SandboxLayer ∩ on the relative-vs-cwd mismatch was the
real denier (NOT the project_root zone anchor).

The fix resolves the path against ``ctx.workspace.base_dir`` before the gate, so
the permission check sees the SAME absolute target the write/read will hit. These
tests pin the round-trip (granted AND lands), the load-bearing sandbox cap
(falsification), and read/write symmetry — with real instances (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.events.events import EventLog
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.file import handle
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import FileIROp
from reyn.workspace.workspace import Workspace


def _ctx(tmp_path: Path, base_dir: Path, *, write_cap: Path, read_cap: Path) -> OpContext:
    """An OpContext mirroring the run-once-in-container scoping: workspace rooted
    on a non-cwd base_dir, config file.write/read=allow (--grant-file-write),
    project_root on the HOST, and a sandbox capping paths to the container repo.
    """
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
            "read_paths": [str(read_cap)],
            "network": False,
        },
    )


@pytest.mark.asyncio
async def test_b3_relative_write_resolves_against_base_dir_and_lands(tmp_path):
    """Tier 2: a relative repo write under a non-cwd base_dir is GRANTED and lands (#187 B3).

    Pre-fix the raw relative path resolved against host cwd → outside the
    sandbox write_paths=[base_dir] cap → denied. Resolving against base_dir
    first makes the gate check /testbed/astropy/... — granted — and the write
    lands under base_dir (round-trip / write-lands, the #1410 lesson).
    """
    testbed = tmp_path / "testbed"
    (testbed / "astropy" / "io").mkdir(parents=True)
    ctx = _ctx(tmp_path, testbed, write_cap=testbed, read_cap=testbed)

    op = FileIROp(kind="file", op="write", path="astropy/io/html.py", content="X = 1\n")
    res = await handle(op, ctx, "control_ir")

    assert res["status"] == "ok"
    # write-lands: the file is under the workspace base_dir, not the host cwd.
    assert (testbed / "astropy" / "io" / "html.py").read_text() == "X = 1\n"


@pytest.mark.asyncio
async def test_b3_sandbox_write_cap_still_load_bearing(tmp_path):
    """Tier 2: ★falsification — when the sandbox write cap does NOT cover the
    base_dir, the same relative write is DENIED. Proves the resolution targets
    base_dir (not a trivially-always-grant) and the SandboxLayer ∩ is intact.
    """
    testbed = tmp_path / "testbed"
    (testbed / "astropy" / "io").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"  # cap excludes the workspace base_dir
    ctx = _ctx(tmp_path, testbed, write_cap=elsewhere, read_cap=testbed)

    op = FileIROp(kind="file", op="write", path="astropy/io/html.py", content="X = 1\n")
    with pytest.raises(PermissionError):
        await handle(op, ctx, "control_ir")


@pytest.mark.asyncio
async def test_b3_relative_read_resolves_against_base_dir(tmp_path):
    """Tier 2: read/write symmetry — a relative read under a tight read_paths cap
    on the base_dir is GRANTED (the read gate also resolves against base_dir).
    """
    testbed = tmp_path / "testbed"
    (testbed / "astropy").mkdir(parents=True)
    (testbed / "astropy" / "io.py").write_text("data = 2\n")
    ctx = _ctx(tmp_path, testbed, write_cap=testbed, read_cap=testbed)

    op = FileIROp(kind="file", op="read", path="astropy/io.py")
    res = await handle(op, ctx, "control_ir")

    assert res.get("status") != "denied"
    # the content read is the file under base_dir (resolved correctly).
    assert "data = 2" in str(res)
