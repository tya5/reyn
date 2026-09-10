"""Tier 2: #5838 段6 -- `cmd` exposed on the LLM `exec` tool schema and the
pipeline `tool:` step (the SAME schema, per the design thread's own ①: "面は
2つではなく1つ" -- `_EXEC_PARAMETERS` feeds both surfaces, so this file
witnesses that sharing rather than re-implementing a second exposure).

Real ``ToolContext``/``Workspace``/``EventLog`` throughout -- no mocks
(CLAUDE.md testing policy). ``cmd``-mode's own parse/policy accept-reject
matrix is covered by its OWN files (``tests/security/test_5838_exec_plan_
parser.py``, ``tests/security/test_5838_stage3_exec_plan_policy.py``) and
the op-level wiring by ``tests/core/test_5838_stage4_shc_exec.py`` -- this
file is scoped to what 段6 ADDS: the tool-schema surface, the handler's own
XOR/async tool-error gates (never a raw ``KeyError``), the shared-schema
witness, the `deny_subprocess` denial-classification confirmation, and the
R4 pipeline-resume confirmation (issue #5838's own acceptance items ②③④⑥).

⚠️ **Disclosed gap**: `argv0_resolved`/`plan` field coverage (段5) is NOT
re-proven here -- already covered by `test_5838_stage4_shc_exec.py`'s own
`test_a_chained_cmd_records_each_segments_resolved_argv0_as_plan` et al.
This file's own `started`-event assertion (below) checks only the NEW
surface: that the ORIGINAL `cmd` text still reaches the event unchanged
when routed through the TOOL layer (`EXEC.handler`), not only through a
directly-constructed op (the stage4 file's own path).
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.data.workspace.workspace import Workspace
from reyn.tools.exec import EXEC
from reyn.tools.pipeline_verbs import _make_tool_dispatch
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.events import collect_events, settle


def _tool_ctx(tmp_path: Path, *, caller_kind: str = "operator", router_state=None) -> "tuple[ToolContext, list]":
    events = EventLog()
    collected = collect_events(events)
    ws = Workspace(events=events, base_dir=tmp_path)
    ctx = ToolContext(
        events=events, permission_resolver=None, workspace=ws,
        caller_kind=caller_kind, router_state=router_state,
    )
    return ctx, collected


# ─── schema: argv no longer required, cmd is a real property ─────────────


def test_schema_relaxes_required_argv_and_adds_cmd():
    """Tier 1: contract -- `EXEC.parameters` (the SAME object both the
    router LLM surface and the pipeline `tool:` step validate against,
    see the shared-schema witness below) no longer names `argv` in
    `required`, and carries a `cmd` string property. NOT expressed as
    `oneOf`/`anyOf` (architect ruling, #5838 thread) -- this pins the
    ABSENCE of that shape, since a later change re-adding it would
    silently reintroduce the enforcement-gap risk the ruling rejected."""
    params = EXEC.parameters
    assert "argv" not in params["required"]
    assert params["properties"]["cmd"]["type"] == "string"
    assert "oneOf" not in params
    assert "anyOf" not in params


# ─── XOR: tool error (never KeyError), with sibling real successes ───────


@pytest.mark.asyncio
async def test_xor_violation_is_a_tool_error_naming_both_params_with_working_siblings(tmp_path):
    """Tier 2: giving BOTH argv and cmd, or NEITHER, returns a tool-error
    RESULT (never an exception -- the exact `KeyError` #5838 段6's own ②
    exists to prevent) whose message names which parameter to use. Sibling
    assertions in this SAME test (issue #5838's own strip-falsify
    requirement): `cmd` alone and `argv` alone each run for real and
    succeed -- so the gate cannot pass by rejecting everything."""
    ctx, collected = _tool_ctx(tmp_path)

    both = await EXEC.handler({"argv": ["echo", "hi"], "cmd": "echo hi"}, ctx)
    assert both["status"] == "error"
    assert "argv" in both["error"] and "cmd" in both["error"]

    neither = await EXEC.handler({}, ctx)
    assert neither["status"] == "error"
    assert "argv" in neither["error"] and "cmd" in neither["error"]

    # sibling: cmd alone actually runs.
    cmd_ok = await EXEC.handler({"cmd": "echo hi"}, ctx)
    assert cmd_ok["status"] == "ok"
    assert cmd_ok["stdout"].strip() == "hi"

    # sibling: argv alone actually runs.
    argv_ok = await EXEC.handler({"argv": ["/bin/echo", "hi"]}, ctx)
    assert argv_ok["status"] == "ok"
    assert argv_ok["stdout"].strip() == "hi"

    # #5838 段7 confirmation, through the TOOL layer (not a directly-
    # constructed op, unlike test_5838_stage4_shc_exec.py's own witness):
    # the started event's argv[2] is the ORIGINAL cmd text verbatim --
    # the corpus source #5987 depends on (issue #5838 thread, architect
    # ⑥).
    await settle(ctx.events)
    started = [e for e in collected if e.type == "sandboxed_exec_started"]
    (cmd_started,) = [e for e in started if e.data["argv"][0] == "/bin/sh"]
    assert cmd_started.data["argv"] == ["/bin/sh", "-c", "echo hi"]


# ─── cmd x collect="async": tool error, with sibling argv x async success ─


@pytest.mark.asyncio
async def test_cmd_with_async_is_a_tool_error_and_argv_async_still_works(tmp_path):
    """Tier 2: `cmd` has no async leg (`run_exec_async`'s own signature is
    `argv: list[str]`, no `cmd` -- #5838 段6's own ③, a deliberate scope
    boundary). `exec(cmd=..., collect="async")` returns a tool error
    naming `argv` as the async-capable alternative, never a raw `KeyError`
    from the async branch's own `args["argv"]` read. Sibling in this SAME
    test: `argv` + `collect="async"` reaches the REAL async path (a real
    `AgentRegistry`/`Session`, mirroring `test_run_exec_async_4733.py`'s
    own construction -- no mock stands in for the async dispatch)."""
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session_api import run_exec_async
    from reyn.security.sandbox.noop_backend import NoopBackend
    from tests._support.agent_session import make_session

    ctx, _collected = _tool_ctx(tmp_path)

    cmd_async = await EXEC.handler({"cmd": "echo hi", "collect": "async"}, ctx)
    assert cmd_async["status"] == "error"
    assert "argv" in cmd_async["error"]
    assert "async" in cmd_async["error"]

    # sibling: argv + collect="async" reaches the real async dispatch.
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile):
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), sandbox_backend=NoopBackend(),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    caller = reg.get_or_load("alpha")

    async_fn = functools.partial(run_exec_async, reg, caller_agent="alpha", caller_sid="main")
    async_ctx, _ = _tool_ctx(
        tmp_path, caller_kind="router",
        router_state=RouterCallerState(sandboxed_exec_async_fn=async_fn),
    )

    argv_async = await EXEC.handler(
        {"argv": [sys.executable, "-c", "print('hi')"], "collect": "async"}, async_ctx,
    )
    assert argv_async["status"] == "started"
    task_id = argv_async["data"]["task_id"]

    import asyncio

    while caller.chains.has(task_id):
        await asyncio.sleep(0)


# ─── shared schema: the pipeline tool: step passes cmd through the SAME schema ─


@pytest.mark.asyncio
async def test_pipeline_tool_step_cmd_passes_through_the_shared_schema(tmp_path):
    """Tier 2: #5838 段6's own ① -- a pipeline `tool:` step dispatching
    `exec` with `cmd` is validated against the SAME `EXEC.parameters`
    schema the router LLM surface uses (`dispatch_tool`'s own arg-schema
    check, `core/dispatch/dispatcher.py`), not a second, pipeline-specific
    exposure. Witnesses the sharing -- does NOT re-implement or re-prove
    the schema's own accept/reject matrix (covered above)."""
    ctx, _collected = _tool_ctx(tmp_path, caller_kind="pipeline")
    dispatch = _make_tool_dispatch(ctx)

    result = await dispatch("exec", {"cmd": "echo hi"})
    assert result["status"] == "ok"
    assert result["stdout"].strip() == "hi"

    # the XOR tool-error gate applies identically through this path too --
    # further evidence there is no second exposure with its own rules.
    both = await dispatch("exec", {"argv": ["echo", "hi"], "cmd": "echo hi"})
    assert both["status"] == "error"
    assert "argv" in both["error"] and "cmd" in both["error"]


# ─── deny_subprocess x pipe in cmd: classified, not a raw fork leak ──────


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
@pytest.mark.asyncio
async def test_deny_subprocess_pipe_in_cmd_is_classified_not_a_raw_fork_leak(tmp_path):
    """Tier 2: darwin-only -- see `test_seatbelt_subprocess_enforcement_
    1914.py`'s own CI-visibility-gap note, same class here. `deny_
    subprocess: True` + a pipe in `cmd` makes the SHELL ITSELF fork() for
    the pipe, which the sandbox denies at the launcher layer (#5838 段6's
    own ④) -- `classify_denial` (`security/sandbox/denial.py`) is a PURE
    function of (returncode, stderr), unrelated to argv-vs-cmd mode, so
    this confirms (does not newly build) that the EXISTING classification
    already covers the cmd-mode shape: `denial_class == "fork_denied"`,
    never a bare, unclassified `fork: Operation not permitted` reaching
    the LLM-facing canonical text."""
    from reyn.core.offload.canonical import sandboxed_exec_to_canonical
    from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend

    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    ctx, _collected = _tool_ctx(tmp_path, router_state=RouterCallerState(sandbox_backend="seatbelt"))
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.sandboxed_exec import handle as handle_sandboxed_exec
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl

    op_ctx = OpContext(
        workspace=ctx.workspace, events=ctx.events, permission_decl=PermissionDecl(),
        permission_resolver=None,
        default_sandbox_policy={"deny_subprocess": True},
        sandbox_backend=backend,
    )
    op = SandboxedExecIROp(kind="sandboxed_exec", cmd="/bin/echo hi | /usr/bin/true")

    result = await handle_sandboxed_exec(op=op, ctx=op_ctx)

    assert result["denial_class"] == "fork_denied", (
        f"the pipe-fork denial must classify as fork_denied, same as the bare-command "
        f"shape test_seatbelt_subprocess_enforcement_1914.py already covers: {result}"
    )

    canonical = sandboxed_exec_to_canonical(result)
    # The classified note is PREPENDED ahead of the raw stderr -- the LLM
    # never sees the bare, unexplained syscall error as the FIRST thing.
    assert canonical["text"].startswith("[sandbox]"), (
        f"a classified denial must lead with the named-class explanation, not the "
        f"raw stderr: {canonical['text']!r}"
    )
    assert canonical["meta"].get("denial_class") == "fork_denied"
