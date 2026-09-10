"""Tier 2: #6008/#6058 -- `ambient_path()` (`sandbox/backend.py`) and the
per-operation thread it feeds.

#6008's own invariant (STILL true, unchanged by #6058): within one
`sandboxed_exec` operation, the `PATH` `check_exec_plan_policy` (policy)
checked and the `PATH` the real child's own env resolves against (exec)
must be the IDENTICAL value, never two independent `os.environ` reads
straddling the policy `await` (#5838's own invariant: the value policy
checked is the value exec uses).

#6058 (this file's revision) removed HOW #6008 closed that gap: a
module-level, process-LIFETIME memo inside `ambient_path()` itself, which
made every caller for the rest of the process converge on whatever was
read FIRST -- including a later test's own `monkeypatch.setenv("PATH",
...)`, which the memo made invisible to policy/exec, and which made
`tests/security/test_sandbox_argv0_resolve_2820.py` pass or fail by
`pytest -n auto` worker/test ORDERING rather than by any code under test
(#6058's own filed reproduction). The fix keeps #6008's "same value within
one operation" guarantee but re-scopes its LIFETIME to one operation: read
once at the operation's entry (`sandboxed_exec.py`'s own `env_path =
ambient_path()`), thread that single value down explicitly as the
`env_path` parameter on `check_exec_plan_policy`/`SandboxBackend.run`/
`SandboxBackend.wrap_command`, never through a global both sides
independently re-consult.

#6063 (lead-coder BLOCKING co-vet, issuecomment-5612883218): #6058's own
first cut threaded `env_path` down as a parameter but left it OPTIONAL
(`= None`) on every backend `run()`/`wrap_command()`, with each backend
falling back to its OWN `ambient_path()` read on omission -- the exact
#6008 shape (two independent reads that can diverge), reopened via a new
path (an omitted parameter, invisible to CI, only visible when a caller
genuinely forgets to thread the value through) instead of the old one (a
process-lifetime cache). This revision removes that fallback and makes
`env_path` a REQUIRED keyword-only parameter on every backend's
`run()`/`wrap_command()` (and on `run_and_classify`) -- an omitted
argument is now a `TypeError` at the call site, not a silent independent
re-read inside the backend. `None` remains a legitimate VALUE ("the
caller read `PATH` and it was genuinely unset"), never a way to ask the
backend to re-derive it.

Real ``OpContext``/``EventLog``/``Workspace`` + the REAL default sandbox
backend throughout for the end-to-end witness below -- no mocks (CLAUDE.md
testing policy). Mirrors ``tests/core/test_5838_stage4_shc_exec.py``'s own
``test_the_real_childs_path_matches_sandboxed_execs_own_env_path`` (#6007),
which this issue's own module docstring names as the exact gap: that test
observed the two sides HAPPENED to agree; this file adds the test that a
change occurring BETWEEN the two reads no longer causes them to diverge
WITHIN one operation -- while a change occurring BETWEEN two separate
operations (or two bare calls to ``ambient_path()`` with nothing in
between) is now visible, not frozen forever (the #6058 acceptance
witness, a deterministic reproduction of the architect's own filed
scenario -- not reliant on `-n auto` worker/test ordering the way
`test_sandbox_argv0_resolve_2820.py` was)."""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import SandboxedExecIROp
from reyn.security.permissions.permissions import PermissionDecl
from reyn.security.sandbox.backend import ambient_path


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
        # child to be that fallback -- the exact branch #6008/#6058 changed
        # -- so this test actually exercises the fixed code, not passthrough.
        default_sandbox_policy={"env_deny_names": ["PATH"]},
        contextual_permission=None,
        sandbox_backend=None,
    )
    return ctx, []


def test_ambient_path_has_no_process_lifetime_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the #6058 acceptance witness -- a deterministic reproduction
    of the architect's own filed scenario (issue #6058/#6059): call
    `ambient_path()` once, REWRITE `os.environ["PATH"]`, call it again --
    the SECOND call must see the NEW value, not the first one. This is the
    exact shape #6008's process-lifetime memo broke (it would have
    returned the FIRST value here, poisoned for the rest of the process)
    and the exact shape that made CI's `-n auto` worker/test ordering
    decide `test_sandbox_argv0_resolve_2820.py`'s own `monkeypatch.setenv`
    outcome rather than any code a PR changed.

    Deterministic, not order-dependent: this test makes its own two reads
    and its own environment change, in a known sequence within itself --
    it does not depend on which other test ran first in this worker.

    Strip-falsifier: reintroduce a module-level memo inside
    `ambient_path()` (#6008's own original shape) and this goes red -- the
    second call would return the FIRST value instead of the rewritten
    one."""
    monkeypatch.setenv("PATH", "/first/real/path")
    first = ambient_path()
    assert first == "/first/real/path"

    monkeypatch.setenv("PATH", "/tmp/fake-shims:/first/real/path")
    second = ambient_path()

    assert second == "/tmp/fake-shims:/first/real/path", (
        "ambient_path() must see a PATH rewrite that happens between two "
        "calls -- a process-lifetime memo would freeze it on the FIRST "
        "value instead"
    )
    assert second != first


@pytest.mark.asyncio
async def test_a_path_change_during_the_policy_await_no_longer_diverges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the load-bearing end-to-end witness (#6008's own acceptance,
    STILL required after #6058 -- "the value policy used and the real
    child's own $PATH are the same, observed from a real subprocess, both
    sides measured, neither side re-deriving the other") -- the ONE
    property #6058 must not have broken while re-scoping the memo's
    lifetime.

    Reproduces #6008's own original hazard directly: a plugin/third-party
    library sharing this process mutates `os.environ["PATH"]` during the
    real suspension point `run_sandboxed_exec` already has between its
    policy check (`check_exec_plan_policy`, an `await`) and the backend's
    own `run()` (#5838 段4's cmd-mode path, the one #6008 was filed
    against) -- simulated here by monkeypatching `check_exec_plan_policy`
    itself to mutate `PATH` right after it returns, inside the SAME
    `run_sandboxed_exec` call. `env_path` was read ONCE, before the
    policy check, and must still be the value threaded into the backend's
    own env-building -- never a fresh read taken after the mutation.

    Strip-falsifier: revert `run_sandboxed_exec`/`run_and_classify`/each
    backend's `run()` to calling `ambient_path()` directly again instead
    of threading the single `env_path` read down as a parameter, and this
    goes red on Linux/macOS CI -- the spawned child's own `$PATH` would
    show the MUTATED value instead (two independent reads straddling the
    policy `await`, #6008's original bug)."""
    from reyn.security import exec_plan_policy as exec_plan_policy_module

    monkeypatch.setenv("PATH", "/original/marker/path:/usr/bin:/bin")
    original_check = exec_plan_policy_module.check_exec_plan_policy

    async def _check_then_mutate_path(*args: object, **kwargs: object) -> object:
        result = await original_check(*args, **kwargs)  # type: ignore[arg-type]
        # The exact window #6008 exists for: something else sharing this
        # process mutates PATH between the policy check returning and the
        # backend's own run() being invoked.
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        return result

    monkeypatch.setattr(exec_plan_policy_module, "check_exec_plan_policy", _check_then_mutate_path)

    ctx, _ = _make_ctx(tmp_path)
    op = SandboxedExecIROp(kind="sandboxed_exec", cmd="env")
    result = await execute_op(op, ctx)

    assert result["status"] == "ok"
    assert "PATH=/original/marker/path:/usr/bin:/bin" in result["stdout"], (
        "the real child's own $PATH must match the value env_path carried "
        "into the policy check, not the value PATH was mutated to during "
        "the policy await"
    )
    assert "PATH=/usr/bin:/bin\n" not in result["stdout"]


def test_wrap_command_omitting_env_path_is_a_type_error_not_a_silent_fallback() -> None:
    """Tier 2: #6063 BLOCKING co-vet -- omitting `env_path` at a backend
    `wrap_command()` call site must be a `TypeError` (missing required
    keyword-only argument), never a silent independent `ambient_path()`
    read inside the backend.

    This is the direction #6058's own first cut got wrong: `env_path` was
    threaded as a PARAMETER but stayed OPTIONAL (`= None`) on every
    backend, so a caller that forgot to pass it fell back to the
    backend's own uncached read -- the exact #6008 defect (policy's read
    and exec's read can diverge within one operation), reopened via an
    omitted argument instead of a process-lifetime cache, and invisible
    to CI because the fallback always "worked" (just not WITH the value
    policy saw).

    Strip-falsifier: reintroduce `env_path: "str | None" = None` on
    `NoopBackend.wrap_command` (restoring the default this test asserts
    is GONE) and this goes green for the wrong reason -- no `TypeError`
    would be raised at all.
    """
    from reyn.security.sandbox.noop_backend import NoopBackend
    from reyn.security.sandbox.policy import SandboxPolicy

    backend = NoopBackend()
    with pytest.raises(TypeError, match="env_path"):
        backend.wrap_command(["echo", "hi"], SandboxPolicy())  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_run_omitting_env_path_is_a_type_error_not_a_silent_fallback() -> None:
    """Tier 2: #6063 BLOCKING co-vet -- same direction as the
    `wrap_command()` sibling above, for `SandboxBackend.run()`. Omitting
    `env_path` must be a `TypeError` at the call site, never a silent
    independent `ambient_path()` read inside the backend.

    Strip-falsifier: reintroduce `env_path: "str | None" = None` on
    `NoopBackend.run` and this goes green for the wrong reason -- no
    `TypeError` would be raised at all (the backend would silently fall
    back to its own uncached `ambient_path()` read instead)."""
    from reyn.security.sandbox.noop_backend import NoopBackend
    from reyn.security.sandbox.policy import SandboxPolicy

    backend = NoopBackend()
    with pytest.raises(TypeError, match="env_path"):
        await backend.run(["echo", "hi"], SandboxPolicy())  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_run_and_classify_omitting_env_path_is_a_type_error() -> None:
    """Tier 2: #6063 BLOCKING co-vet -- `run_and_classify` (the shared
    resolve/run/classify tail every agent-reachable launch route uses,
    `sandbox/launcher.py`) must also refuse a missing `env_path` at the
    type level, the same as the backend methods it forwards to -- it is
    itself a caller-facing seam where a forgotten `env_path` would
    otherwise forward `None`-by-omission straight into `backend.run()`.

    Strip-falsifier: reintroduce `env_path: "str | None" = None` on
    `run_and_classify` and this goes green for the wrong reason."""
    from reyn.security.sandbox.launcher import run_and_classify
    from reyn.security.sandbox.noop_backend import NoopBackend
    from reyn.security.sandbox.policy import SandboxPolicy

    backend = NoopBackend()
    with pytest.raises(TypeError, match="env_path"):
        await run_and_classify(backend, ["echo", "hi"], SandboxPolicy())  # type: ignore[call-arg]
