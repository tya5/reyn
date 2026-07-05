"""Tier 2: #2593 — the pipeline DSL `shell` step, implemented via `sandboxed_exec`.

Before this PR, `shell: {command, ...}` parsed to `ToolStep(name="shell")` but no
"shell" tool was registered — every `shell` step failed at dispatch (100% fail).
The fix (locked design, #2593): `shell` is tool-step SUGAR — the command is
STATIC; the previous step's pipe-data is JSON-encoded onto the process's STDIN;
STDOUT becomes the step's output. Implemented as thin sugar over the EXISTING
`sandboxed_exec` op (no new subprocess handling): `reyn.tools.shell._handle`
builds a `SandboxedExecIROp` and delegates to
`reyn.core.op_runtime.sandboxed_exec.handle` via the SAME `ToolContext` →
`OpContext` bridge `reyn.tools.sandboxed_exec._handle` uses.

Covers:
1. registry wiring: `registry.lookup("shell")` resolves (the static-analysis
   gate's check 3 + the executor's bare-name tool_dispatch fallback both need this).
2. happy path: a real `PipelineExecutor.run`, through the real
   `pipeline_verbs._make_tool_dispatch`, with a `NoopBackend`-injected
   `ToolContext` — the previous step's pipe-data reaches the shell command's
   STDIN (a `cat`-like command echoes it back), and STDOUT becomes the step's
   output/pipe-data.
3. `verify: schema` on a shell step's STDOUT (conforming passes, violating fails).
4. `timeout` maps to `SandboxedExecIROp.timeout_seconds` (a `sleep`-past-timeout
   command returns quickly with status "timeout", not blocking the 60s default).
5. SECURITY regression: the untrusted + delegate floors DENY "shell" (mirrors
   the `exec__sandboxed_exec` floor regression) at the REAL contextual gate.
6. round-trip: a `shell` DSL step round-trips through the parser with its
   `ExprRef("pipe")` stdin-threading arg, static `command`, and `timeout`.

No mocks of collaborators — real EventLog / Workspace / NoopBackend /
PipelineExecutor / SchemaRegistry / CapabilityProfile resolution throughout.
"""
from __future__ import annotations

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.pipeline.executor import (
    ExprRef,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.parser import parse_pipeline_dsl
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.registry import AgentRegistry
from reyn.security.permissions.capability_profile import (
    builtin_untrusted_profile,
    resolve_profile,
)
from reyn.security.permissions.effective import tool_contextually_denied
from reyn.security.permissions.permissions import PermissionDecl
from reyn.security.sandbox import NoopBackend
from reyn.tools import RouterCallerState, ToolContext, get_default_registry
from reyn.tools.pipeline_verbs import _make_tool_dispatch

# ─── shared fixture helper ───────────────────────────────────────────────────


def _noop_tool_context() -> ToolContext:
    """A real ToolContext whose op_context_factory yields an OpContext pinned to
    NoopBackend — deterministic sandbox execution for the test (no platform
    Seatbelt/Landlock variance)."""
    events = EventLog()
    ws = Workspace(events=events)

    def _factory() -> OpContext:
        return OpContext(
            workspace=ws,
            events=events,
            permission_decl=PermissionDecl(),
            permission_resolver=None,
            sandbox_backend=NoopBackend(),
        )

    return ToolContext(
        events=events,
        permission_resolver=None,
        workspace=ws,
        caller_kind="router",
        router_state=RouterCallerState(op_context_factory=_factory),
    )


# ─── 1. registry wiring ──────────────────────────────────────────────────────


def test_shell_resolves_in_the_default_registry():
    """Tier 2: registry.lookup("shell") resolves — the #2593 bug was exactly
    that this returned None (ToolStep(name="shell") had no registered tool)."""
    registry = get_default_registry()
    assert registry.lookup("shell") is not None


# ─── 2. happy path: real PipelineExecutor + real dispatch + NoopBackend ─────


@pytest.mark.asyncio
async def test_shell_step_threads_pipe_data_to_stdin_and_stdout_to_output():
    """Tier 2: a shell step's STDIN receives the previous step's pipe-data
    JSON-encoded (proven via `cat`, which echoes stdin verbatim to stdout), and
    its STDOUT becomes the step's own pipe-data / output."""
    ctx = _noop_tool_context()
    pipeline = Pipeline(
        steps=[
            TransformStep(value="{n: 3, msg: 'hi'}", output="seed"),
            ToolStep(
                name="shell",
                args={"command": "cat", "stdin_pipe": ExprRef("pipe")},
                output="echoed",
            ),
        ]
    )
    result = await PipelineExecutor().run(
        pipeline,
        {},
        tool_dispatch=_make_tool_dispatch(ctx),
        state_log=None,
        run_id="run-shell-stdin",
    )
    assert result.pipe_data == {"n": 3, "msg": "hi"}
    assert result.named_stores["echoed"] == {"n": 3, "msg": "hi"}


# ─── 3. verify: schema on shell output ──────────────────────────────────────


@pytest.mark.asyncio
async def test_shell_step_schema_verify_passes_conforming_output():
    """Tier 2: a shell step's STDOUT, when JSON-shaped, passes a matching
    `verify: schema` through the REAL SchemaRegistry/validate path."""
    ctx = _noop_tool_context()
    registry = SchemaRegistry()
    registry.register("greeting", {"fields": {"msg": {"type": "string", "required": True}}})
    pipeline = Pipeline(
        steps=[
            ToolStep(
                name="shell",
                args={"command": "echo '{\"msg\": \"hello\"}'", "stdin_pipe": ExprRef("pipe")},
                output="out",
                schema="greeting",
            ),
        ]
    )
    result = await PipelineExecutor().run(
        pipeline,
        {},
        tool_dispatch=_make_tool_dispatch(ctx),
        state_log=None,
        run_id="run-shell-schema-ok",
        schema_registry=registry,
    )
    assert result.pipe_data == {"msg": "hello"}


@pytest.mark.asyncio
async def test_shell_step_schema_verify_fails_violating_output():
    """Tier 2: non-conforming STDOUT fails the step's `verify: schema` — the
    violation is never silently swallowed."""
    ctx = _noop_tool_context()
    registry = SchemaRegistry()
    registry.register("greeting", {"fields": {"msg": {"type": "string", "required": True}}})
    pipeline = Pipeline(
        steps=[
            ToolStep(
                name="shell",
                args={"command": "echo '{\"nope\": 1}'", "stdin_pipe": ExprRef("pipe")},
                output="out",
                schema="greeting",
            ),
        ]
    )
    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            pipeline,
            {},
            tool_dispatch=_make_tool_dispatch(ctx),
            state_log=None,
            run_id="run-shell-schema-bad",
            schema_registry=registry,
        )


# ─── 4. timeout maps to timeout_seconds ─────────────────────────────────────


@pytest.mark.asyncio
async def test_shell_step_timeout_maps_to_sandboxed_exec_timeout_seconds():
    """Tier 2: a shell step's `timeout` arg reaches `SandboxedExecIROp.timeout_seconds`
    — a `sleep 5` command with `timeout: 1` returns promptly (NoopBackend's
    wall-clock enforcement fires at 1s), instead of blocking the 60s default."""
    ctx = _noop_tool_context()
    pipeline = Pipeline(
        steps=[
            ToolStep(
                name="shell",
                args={"command": "sleep 5", "stdin_pipe": ExprRef("pipe"), "timeout": 1},
                output="out",
            ),
        ]
    )
    import asyncio
    import time

    start = time.monotonic()
    await asyncio.wait_for(
        PipelineExecutor().run(
            pipeline,
            {},
            tool_dispatch=_make_tool_dispatch(ctx),
            state_log=None,
            run_id="run-shell-timeout",
        ),
        timeout=10,
    )
    elapsed = time.monotonic() - start
    assert elapsed < 5, (
        f"shell step took {elapsed:.1f}s — timeout=1 did not reach "
        "SandboxedExecIROp.timeout_seconds (fell back to the 60s default)"
    )


# ─── 5. SECURITY regression: exec floor parity ──────────────────────────────


def test_untrusted_floor_denies_shell():
    """Tier 2: #2593 exec-floor parity — the #1827 untrusted-content floor denies
    "shell" at the REAL contextual gate, mirroring exec__sandboxed_exec's own
    floor denial (same subprocess-exec threat surface)."""
    contextual, _ = resolve_profile(builtin_untrusted_profile())
    assert tool_contextually_denied(contextual, "shell")


def test_delegate_floor_denies_shell(tmp_path):
    """Tier 2: #2593 exec-floor parity — the #2081 unbound-delegate floor (under
    delegation.capability_default=deny) denies "shell" via the REAL registry
    resolution path (resolved_profile_for) → the real gate seam."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None,
        delegation_capability_default="deny",
    )
    contextual, _ = reg.resolved_profile_for("worker", is_delegate=True)
    assert contextual is not None
    assert tool_contextually_denied(contextual, "shell")


def test_inherit_allows_shell(tmp_path):
    """Tier 2: regression guard — under capability_default=inherit (the default),
    an unbound delegate gets NO floor → "shell" is allowed (the floor is what
    denies; over-denying under inherit would break byte-identical pre-#2081
    behavior for every OTHER floored tool too)."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None,
        delegation_capability_default="inherit",
    )
    contextual, _ = reg.resolved_profile_for("worker", is_delegate=True)
    assert not tool_contextually_denied(contextual, "shell")


# ─── 6. round-trip: shell DSL parse ─────────────────────────────────────────


def test_shell_dsl_round_trips_command_timeout_and_pipe_stdin():
    """Tier 1: a `shell` DSL step with a non-default `timeout` round-trips through
    the parser: the static `command`, the `timeout`, and the `ExprRef("pipe")`
    stdin-threading arg all persist in the parsed `ToolStep`."""
    dsl = """
pipeline: shell-roundtrip
steps:
  - shell: {command: "grep -c foo", timeout: 45, output: matches}
"""
    pipeline = parse_pipeline_dsl(dsl, SchemaRegistry())
    step = pipeline.steps[0]
    assert isinstance(step, ToolStep)
    assert step.name == "shell"
    assert step.args["command"] == "grep -c foo"
    assert step.args["stdin_pipe"] == ExprRef("pipe")
    assert step.args["timeout"] == 45
    assert step.output == "matches"
