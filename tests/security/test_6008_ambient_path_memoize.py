"""Tier 2: #6008 -- `ambient_path()` (`sandbox/backend.py`) reads the
process's own ambient PATH exactly once and memoizes it, so
`check_exec_plan_policy` (policy) and a sandbox backend's own env-building
(exec) -- previously two independent `os.environ` reads separated by a
real suspension point (the policy `await`) -- structurally converge on the
SAME value, never two different ones (#5838's own invariant: the value
policy checked is the value exec uses).

Real ``OpContext``/``EventLog``/``Workspace`` + the REAL default sandbox
backend throughout for the end-to-end witness below -- no mocks (CLAUDE.md
testing policy). Mirrors ``tests/core/test_5838_stage4_shc_exec.py``'s own
``test_the_real_childs_path_matches_sandboxed_execs_own_env_path`` (#6007),
which this issue's own module docstring names as the exact gap: that test
observed the two sides HAPPENED to agree; this file adds the test that a
change occurring BETWEEN the two reads no longer causes them to diverge.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import reyn.security.sandbox.backend as backend_module
from reyn.core.events.events import EventLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import SandboxedExecIROp
from reyn.security.permissions.permissions import PermissionDecl
from reyn.security.sandbox.backend import ambient_path


@pytest.fixture(autouse=True)
def _reset_ambient_path_cache():
    """#6008: `ambient_path()`'s cache is a module-level, process-lifetime
    flag+value pair by design (memoize-once, not per-call) -- without
    resetting it, whichever test in the WHOLE pytest session calls it
    FIRST freezes the value for every other test in the same process,
    silently defeating any test here that means to observe a fresh read."""
    backend_module._ambient_path_read = False
    backend_module._ambient_path_cache = None
    yield
    backend_module._ambient_path_read = False
    backend_module._ambient_path_cache = None


def _make_ctx(tmp_path: Path) -> "tuple[OpContext, list]":
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    events = EventLog()
    ws = Workspace(events=events, base_dir=project_root)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        # env_deny_names=["PATH"]: resolve_passthrough_env (#3901 PR-B ④)
        # copies the WHOLE of live os.environ by default, PATH included --
        # so a default policy's child would get PATH from LIVE os.environ
        # directly, never reaching ambient_path()'s own fallback branch at
        # all. Denying PATH here forces the ONLY source of PATH in the
        # child to be that fallback -- the exact branch #6008 changed --
        # so this test actually exercises the fixed code, not passthrough.
        default_sandbox_policy={"env_deny_names": ["PATH"]},
        contextual_permission=None,
        sandbox_backend=None,
    )
    return ctx, []


def test_ambient_path_reads_the_real_environment_exactly_once_and_memoizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the accessor's own core claim, isolated from any caller.
    First call returns the REAL, currently-set PATH; a SUBSEQUENT
    environment change is invisible to every later call in this process.

    Strip-falsifier: revert `ambient_path()` to a bare
    `os.environ.get("PATH")` (no memoization) and this goes red -- the
    second call would return the newly-set value instead of the first
    one."""
    monkeypatch.setenv("PATH", "/original/marker/path")
    first = ambient_path()
    assert first == "/original/marker/path"

    monkeypatch.setenv("PATH", "/a/completely/different/path")
    second = ambient_path()

    assert second == first, (
        "a PATH change after the first read must not be visible to a "
        "later caller -- that is the whole point of memoizing"
    )
    assert second != "/a/completely/different/path"


@pytest.mark.asyncio
async def test_a_path_change_between_policys_read_and_execs_read_no_longer_diverges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the load-bearing end-to-end witness (#6008's own acceptance
    -- "the value policy used and the real child's own $PATH are the
    same, observed from a real subprocess, both sides measured, neither
    side re-deriving the other").

    Simulates the exact hazard #6008 closes: `ambient_path()` is warmed
    (as `run_sandboxed_exec`'s own policy-check path would do) BEFORE a
    real environment change (a plugin/third-party library sharing this
    process, mutating PATH after policy already read it) -- then a REAL
    command is spawned through the REAL default sandbox backend, and its
    OWN reported `$PATH` (a genuinely independent measurement: the
    backend's own `resolve_passthrough_env` + PATH-fallback plumbing,
    not a second call to `ambient_path()`) must still equal the WARMED
    value, never the value PATH was changed to afterward.

    Strip-falsifier: revert the 5 call sites in `noop_backend.py`/
    `seatbelt.py`/`landlock.py`/`sandboxed_exec.py` to raw
    `os.environ.get("PATH")` and this goes red on Linux/macOS CI (no
    memoization left anywhere to freeze the value) -- the spawned
    child's own `$PATH` would show the LATER value instead."""
    monkeypatch.setenv("PATH", "/original/marker/path:/usr/bin:/bin")
    warmed = ambient_path()  # simulates the policy-check path's own read
    assert warmed == "/original/marker/path:/usr/bin:/bin"

    # A plugin/third-party library mutating the ambient environment AFTER
    # policy already read it -- the exact class #6008 exists for.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    ctx, _ = _make_ctx(tmp_path)
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/sh", "-c", "echo $PATH"])
    result = await execute_op(op, ctx)

    assert result["status"] == "ok"
    assert result["stdout"].strip() == warmed, (
        "the real child's own $PATH must match the value policy already "
        "saw before the environment changed, not the post-change value"
    )
