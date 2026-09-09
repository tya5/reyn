"""Tier 2: #5838 段4 -- `sandboxed_exec`'s `cmd` (shell command line) form.

Real ``OpContext`` + real ``EventLog``/``Workspace`` + the real default
backend (or a real, non-mock recording stub for the deny-side tests, where
the whole point is proving the process never spawns) throughout -- no mocks
(CLAUDE.md testing policy). ``cmd``-mode reuses two things already covered
by their OWN test files, exercised here only through the shared seam:
``parse_exec_plan`` (``tests/security/test_5838_exec_plan_parser.py``) and
``check_exec_plan_policy`` (``tests/security/test_5838_stage3_exec_plan_
policy.py``) -- so this file is about the WIRING (policy runs before
spawn, the ORIGINAL string is what actually executes), not re-proving
either of those two modules' own accept/reject matrices.

⚠️ **Disclosed gap**: `env_path`/`cwd` being computed ONCE and reused for
BOTH `check_exec_plan_policy` and the real argv0 resolution (#5991
BLOCKING ③'s point, carried into this stage -- see `sandboxed_exec.py`'s
own module docstring) is a CODE-LEVEL fact (one local computation, two
call sites reading it), verified by reading the diff at review time --
NOT independently witnessed by a black-box test here. A basename-keyed
tool-axis deny (this file's own deny-side tests) resolves identically
regardless of which `cwd`/PATH is fed it for a bare or absolute name, so
no behavioural difference exists to assert on; a genuine divergence
witness would need a version-manager shim fixture (the #2820 part A
scenario `resolve_real_executable` exists for), out of this file's scope.

#6007 BLOCKING ③ (lead-coder co-vet, issuecomment-5594151412) is a
DIFFERENT angle on `env_path`, covered below (`test_the_real_childs_
path_matches_sandboxed_execs_own_env_path`): not "does changing PATH
change the tool-axis verdict" (the disclosed gap above), but "does the
value `env_path` actually holds match what a REAL spawned child's own
PATH turns out to be" -- today it does (`policy.py`'s own
`resolve_passthrough_env` drops PATH by default, and every backend's own
fallback restores it from `os.environ` regardless), but nothing ties the
two together structurally, so a future change on either side could
silently diverge. No code change requested for this finding -- an
OBSERVATION only, with the explicit condition that the assertion must
NOT put `os.environ` on both sides of the same expression (that would
transcribe the implementation and could never go red): the LEFT side
below is a REAL subprocess's own reported `$PATH`, spawned through the
actual default backend (which resolves env via `resolve_passthrough_env`
internally) -- not a second call to the same accessor.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import SandboxedExecIROp
from reyn.security.permissions.effective import ContextualPermission
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.security.sandbox.backend import SandboxResult
from tests._support.events import collect_events, settle
from tests._support.sandbox_backend import FULLY_ENFORCING_AXES


class _RecordingBackend:
    """Real (non-mock) SandboxBackend stub -- records whether/how `run` was
    called, so a deny-before-spawn claim is a WITNESSED fact (`ran is
    False`), not an inference from the absence of stdout."""

    name = "recording-stub"
    enforced_axes = FULLY_ENFORCING_AXES

    def __init__(self) -> None:
        self.ran = False
        self.received_argv: "list[str] | None" = None

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None,
                   hook_process_context=None, sink=None) -> SandboxResult:
        self.ran = True
        self.received_argv = list(argv)
        return SandboxResult(returncode=0, stdout=b"ran", stderr=b"")


def _make_ctx(
    tmp_path: Path,
    *,
    backend=None,
    contextual: "ContextualPermission | None" = None,
    permission_resolver: "PermissionResolver | None" = None,
) -> "tuple[OpContext, list]":
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    events = EventLog()
    collected = collect_events(events)
    ws = Workspace(events=events, base_dir=project_root)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=permission_resolver,
        default_sandbox_policy={},
        contextual_permission=contextual,
        sandbox_backend=backend,
    )
    return ctx, collected


# ─── schema: exactly one of argv / cmd ────────────────────────────────────


def test_op_rejects_both_argv_and_cmd():
    """Tier 2: constructing the op with BOTH set is a construction-time
    error (the model's own validator), never reaches the handler."""
    with pytest.raises(ValueError, match="exactly one"):
        SandboxedExecIROp(kind="sandboxed_exec", argv=["ls"], cmd="ls")


def test_op_rejects_neither_argv_nor_cmd():
    """Tier 2: FP-sibling -- omitting BOTH is the same construction-time
    error, not a silent "runs nothing"."""
    with pytest.raises(ValueError, match="exactly one"):
        SandboxedExecIROp(kind="sandboxed_exec")


def test_op_accepts_cmd_alone():
    """Tier 2: FP gate -- `cmd` alone (argv omitted, defaults to `[]`)
    constructs cleanly."""
    op = SandboxedExecIROp(kind="sandboxed_exec", cmd="ls -la")
    assert op.cmd == "ls -la"
    assert op.argv == []


# ─── accept side: the ORIGINAL string runs, via /bin/sh -c ────────────────


@pytest.mark.asyncio
async def test_cmd_runs_through_bin_sh_dash_c_with_the_original_string(tmp_path: Path) -> None:
    """Tier 2: end-to-end, the REAL default backend -- a `cmd` with an
    internal double space inside quotes (`echo "a  b"`) only survives if
    the shell that runs is handed the ORIGINAL string, not a re-joined
    argv (joining a reconstructed argv with single spaces would collapse
    it to one space) -- the owner ruling (i) witness: execute the string
    exactly, never a reconstruction from the parsed plan."""
    ctx, collected = _make_ctx(tmp_path)
    op = SandboxedExecIROp(kind="sandboxed_exec", cmd='echo "a  b"')

    result = await execute_op(op, ctx)

    assert result["status"] == "ok"
    assert result["stdout"].strip() == "a  b"
    assert result["argv0_resolved"] is not None
    assert result["argv0_resolved"].endswith("/sh")

    await settle(ctx.events)
    (started,) = [e for e in collected if e.type == "sandboxed_exec_started"]
    assert started.data["argv"] == ["/bin/sh", "-c", 'echo "a  b"']


# ─── plan field (#5838 段5) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_chained_cmd_records_each_segments_resolved_argv0_as_plan(
    tmp_path: Path,
) -> None:
    """Tier 2: #5838 段5 -- a cmd-mode run with TWO segments (piped) emits
    `plan` on BOTH started and completed, one `{"argv0": <original>,
    "resolved": <resolved>}` entry per segment, in order -- the ONLY
    place a chained command's actual per-segment binaries are named
    (`argv0_resolved` alone is always `/bin/sh`). The pair shape is
    architect's own PR co-vet suggestion (issuecomment-5594370219): a
    reader must not have to re-parse `cmd` to learn each segment's
    ORIGINAL `argv[0]`. Uses `/bin/echo`/`/usr/bin/true` (absolute,
    portable) so the resolved value is deterministic across machines
    without depending on PATH-search specifics -- here `argv0 ==
    resolved` for both (no shim indirection on an absolute path); the
    sibling test below uses a BARE name specifically to witness the two
    differing."""
    ctx, collected = _make_ctx(tmp_path)
    op = SandboxedExecIROp(kind="sandboxed_exec", cmd="/bin/echo hi | /usr/bin/true")

    result = await execute_op(op, ctx)

    assert result["status"] == "ok"

    await settle(ctx.events)
    (started,) = [e for e in collected if e.type == "sandboxed_exec_started"]
    (completed,) = [e for e in collected if e.type == "sandboxed_exec_completed"]
    expected = [
        {"argv0": "/bin/echo", "resolved": "/bin/echo"},
        {"argv0": "/usr/bin/true", "resolved": "/usr/bin/true"},
    ]
    assert started.data["plan"] == expected
    assert completed.data["plan"] == expected


@pytest.mark.asyncio
async def test_a_bare_name_in_plan_shows_argv0_distinct_from_its_resolved_path(
    tmp_path: Path,
) -> None:
    """Tier 2: #5838 段5 -- a BARE command name (`true`, PATH-searched,
    unlike the absolute-path test above) shows `argv0` (the original
    token) and `resolved` (the absolute path PATH search + shim
    resolution actually produced) as genuinely DIFFERENT strings --
    proving the pair carries independent information, not just the same
    value twice."""
    ctx, collected = _make_ctx(tmp_path)
    op = SandboxedExecIROp(kind="sandboxed_exec", cmd="true")

    result = await execute_op(op, ctx)

    assert result["status"] == "ok"

    await settle(ctx.events)
    (started,) = [e for e in collected if e.type == "sandboxed_exec_started"]
    (entry,) = started.data["plan"]
    assert entry["argv0"] == "true"
    assert entry["resolved"] == "/usr/bin/true"
    assert entry["argv0"] != entry["resolved"]


@pytest.mark.asyncio
async def test_argv_mode_leaves_plan_none_on_both_events(tmp_path: Path) -> None:
    """Tier 2: FP gate, sibling of the test above -- an ordinary argv-mode
    run's `plan` field is `None` on both events, the unchanged shape (a
    single command already has `argv0_resolved`; `plan` is cmd-mode-only,
    lead-coder's own acceptance criterion ②)."""
    ctx, collected = _make_ctx(tmp_path)
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/echo", "hi"])

    result = await execute_op(op, ctx)

    assert result["status"] == "ok"

    await settle(ctx.events)
    (started,) = [e for e in collected if e.type == "sandboxed_exec_started"]
    (completed,) = [e for e in collected if e.type == "sandboxed_exec_completed"]
    assert started.data["plan"] is None
    assert completed.data["plan"] is None


# ─── deny side: policy runs BEFORE the backend ever spawns ────────────────


@pytest.mark.asyncio
async def test_tool_axis_deny_stops_the_run_before_the_backend_spawns(tmp_path: Path) -> None:
    """Tier 2: a `cmd` resolving to a contextually-denied binary denies
    (``execute_op``'s own ``except PermissionError`` -> ``status="denied"``,
    the SAME channel every other permission gate in this codebase uses)
    and the backend's own `run` is never called -- the accept-side sibling
    of the string-fidelity test above; together they
    are the "受入は両方向" lead-coder asked for (approved runs as
    approved / a denied binary is never spawned, not merely reported as
    denied after the fact)."""
    backend = _RecordingBackend()
    ctx, _collected = _make_ctx(
        tmp_path, backend=backend,
        contextual=ContextualPermission(tool_deny=frozenset({"true"})),
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", cmd="true")

    result = await execute_op(op, ctx)

    assert result["status"] == "denied"
    assert backend.ran is False


@pytest.mark.asyncio
async def test_threat_scan_deny_stops_the_run_before_the_backend_spawns(tmp_path: Path) -> None:
    """Tier 2: a `cmd` matching a real block-severity threat pattern
    (reverse_shell_devtcp — a plain substring match, `/dev/tcp/`, so this
    stays inside 段2's supported grammar; the fd-duplication shape a real
    reverse shell usually pairs it with, e.g. `>&`, is a DIFFERENT
    rejection -- 段2's own "unsupported shell construct", not this one)
    also denies before spawn -- the segment-level threat scan (段3) is
    reached from the real op path, not only from ``check_exec_plan_
    policy``'s own unit tests."""
    from reyn.config.chat import ThreatScanConfig

    backend = _RecordingBackend()
    project_root = tmp_path / "proj"
    project_root.mkdir(parents=True, exist_ok=True)
    events = EventLog()
    ws = Workspace(events=events, base_dir=project_root)
    ctx = OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=None, default_sandbox_policy={},
        threat_scan=ThreatScanConfig(), sandbox_backend=backend,
    )
    op = SandboxedExecIROp(
        kind="sandboxed_exec", cmd="echo /dev/tcp/10.0.0.1/4444",
    )

    result = await execute_op(op, ctx)

    assert result["status"] == "denied"
    assert backend.ran is False


@pytest.mark.asyncio
async def test_a_redirect_with_no_permission_resolver_denies_fail_closed(tmp_path: Path) -> None:
    """Tier 2: end-to-end witness of #5991 BLOCKING ①'s fail-closed fix --
    a `cmd` containing a redirect, with no `permission_resolver` wired on
    ``ctx`` (this file's own `_make_ctx` default), denies rather than
    silently running with the redirect target unchecked."""
    backend = _RecordingBackend()
    ctx, _collected = _make_ctx(tmp_path, backend=backend, permission_resolver=None)
    op = SandboxedExecIROp(kind="sandboxed_exec", cmd="echo hi > out.txt")

    result = await execute_op(op, ctx)

    assert result["status"] == "denied"
    assert backend.ran is False


@pytest.mark.asyncio
async def test_an_unparseable_cmd_returns_a_structured_error_not_a_crash(tmp_path: Path) -> None:
    """Tier 2: a `cmd` `parse_exec_plan` (段2) rejects -- e.g. a literal
    ``$`` (variable expansion, always forbidden) -- returns the op's own
    ``status="error"`` envelope with a legible reason, the same shape the
    handler already uses for its other pre-flight validation errors
    (timeout_seconds), never an uncaught exception from ``parse_exec_
    plan`` bubbling out raw."""
    backend = _RecordingBackend()
    ctx, _collected = _make_ctx(tmp_path, backend=backend)
    op = SandboxedExecIROp(kind="sandboxed_exec", cmd="echo $HOME")

    result = await execute_op(op, ctx)

    assert result["status"] == "error"
    assert "could not be parsed" in result["error"]
    assert backend.ran is False


# ─── #6007 BLOCKING ③ (lead-coder): env_path vs. the real child's PATH ────


@pytest.mark.asyncio
async def test_the_real_childs_path_matches_sandboxed_execs_own_env_path(tmp_path: Path) -> None:
    """Tier 2: lead-coder co-vet (issuecomment-5594151412) -- an
    OBSERVATION, not a code change (see this file's own module docstring
    for the full framing). Runs a real command through the REAL default
    backend (argv-mode -- `$PATH` is shell expansion, always rejected by
    `parse_exec_plan`, so this witness deliberately does not go through
    cmd-mode) that echoes its own `$PATH`, and asserts it equals
    `os.environ.get("PATH")` -- the SAME value `run_sandboxed_exec`'s own
    `env_path` local holds, used to resolve argv0 for both request
    shapes. The LEFT side is a genuinely independent measurement (a real
    subprocess's own env, reached through `resolve_passthrough_env` +
    each backend's own PATH fallback), not a second call to the same
    accessor the RIGHT side also calls -- so a future divergence between
    the two (e.g. a backend that stops falling back to `os.environ`, or a
    default `policy.env_deny_names` that starts denying PATH) goes RED
    here rather than passing silently."""
    ctx, _collected = _make_ctx(tmp_path)
    op = SandboxedExecIROp(kind="sandboxed_exec", argv=["/bin/sh", "-c", "echo $PATH"])

    result = await execute_op(op, ctx)

    assert result["status"] == "ok"
    assert result["stdout"].strip() == os.environ.get("PATH")
