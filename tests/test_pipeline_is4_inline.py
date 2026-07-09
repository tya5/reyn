"""Tier 2: IS-4 ad-hoc INLINE pipeline launch + the static-analysis gate.

Covers ``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md`` R6's inline
launch verbs (``run_pipeline_inline`` / ``run_pipeline_inline_async``): an agent
GENERATES a pipeline as a DSL string at runtime, the string is parsed (IS-3) +
statically gated, then run through the SAME crash-recoverable driver-session the
REGISTERED verbs use (IS-2/IS-6). Real collaborators throughout — real
``AgentRegistry``/``Session``/``StateLog``/``PipelineExecutor``/parser; the only
fake is the scripted LLM callable injected through the real ``RouterLoopDriver``
``_loop_observer`` seam (same discipline as ``test_run_pipeline_tool_is1.py``).
No ``MagicMock``/``patch`` of collaborators.

The behaviors, each with a falsifiable assertion:

- **inline happy path (sync + async)**: a generated DSL string parses, passes
  the gate, runs to completion (real tool side effect + a real agent step), and
  the result is returned inline (sync) / delivered via the inbox (async).
- **static gate rejections (all six checks)**: a bad definition is rejected
  with a clear error and NOTHING is spawned (no run dir under
  ``.reyn/pipeline/state/``) — malformed DSL, an unresolvable schema ref, an
  unknown tool, an S3 nested pipeline launch, and (the INLINE-ONLY security
  check) an agent step naming a NON-invoker identity (capability escalation).
- **S3 sibling sweep**: the inline verbs are added to BOTH the pipeline-tool-step
  deny (``_PIPELINE_STEP_DENY_TOOLS``) and the agent-step delegation deny
  (``_DELEGATION_DENY_TOOLS``) — an inline launch is non-grantable inside a
  pipeline (nesting is call-only).
- **inline crash-resume e2e (truncate-falsify)**: an inline run whose generated
  definition was serialized into ``invocation.json`` crash-resumes EXACTLY-ONCE
  after the WAL is truncated below its source events — proving the recovery
  source is the serialized generated Pipeline, not a registry lookup.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import PipelineExecutor
from reyn.core.pipeline.parser import parse_pipeline_dsl
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.core.pipeline.serde import pipeline_to_dict
from reyn.core.pipeline.work_order import (
    PipelineWorkOrder,
    pipeline_run_dir,
    write_invocation,
)
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.tools.pipeline_verbs import (
    _handle_run_pipeline_inline,
    _handle_run_pipeline_inline_async,
    _make_tool_dispatch,
)
from reyn.tools.types import RouterCallerState, ToolContext


class _ScriptedAgentReply:
    """One fixed plain-text turn — the LLM is incidental to what's under test."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls += 1
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _agent_registry(
    tmp_path: Path, state_log: "StateLog", scripted: "_ScriptedAgentReply | None",
) -> AgentRegistry:
    """Real AgentRegistry + real Session factory (mirrors the IS-1/IS-2 tests)."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        # #2708 P3.1: accept + forward the attached driver spawn's present-sink override.
        s = Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,  # #2708 P3.2a: accept + forward the attached driver spawn's intervention bridge
        )
        if scripted is not None:
            s._loop_driver._loop_observer = (
                lambda loop: setattr(loop, "_llm_caller", scripted)
            )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


def _install_write_tool(monkeypatch) -> None:
    """Register a REAL side-effecting tool (append a line per call to the file
    named in ``args['path']`` — workspace-independent). Same monkeypatch idiom as
    the IS-2 test: every lookup still routes through the real
    ``ToolRegistry.register``/``lookup`` contract."""
    import reyn.tools as tools_pkg
    from reyn.tools.types import ToolDefinition, ToolGates

    async def _handler(args, ctx):
        p = Path(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(str(args["content"]) + "\n")
        return {"content": str(args["content"])}

    tool = ToolDefinition(
        name="is4_write",
        description="IS-4 test: append content to a file (real side effect).",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow", phase="allow"),
        handler=_handler,
        category="io",
        purity="side_effect",
    )
    base = tools_pkg.get_default_registry

    def _with_tool():
        registry = base()
        registry.register(tool)
        return registry

    monkeypatch.setattr(tools_pkg, "get_default_registry", _with_tool)


def _bare_ctx(state_log: "StateLog | None" = None) -> ToolContext:
    from reyn.core.events.events import EventLog
    return ToolContext(
        events=EventLog(), permission_resolver=None, workspace=None,
        caller_kind="router", router_state=None, state_log=state_log,
    )


def _ctx(
    reg: AgentRegistry, caller: Session, state_log: "StateLog | None",
    *, wired: bool = True,
) -> ToolContext:
    """A full router ToolContext (agent_registry + host + WAL) — inline needs no
    PipelineRegistry. ``wired=False`` drops agent_registry/host to exercise the
    wiring-error path."""
    return ToolContext(
        events=caller._router_host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            agent_registry=reg if wired else None,
            host=caller._router_host if wired else None,
        ),
        state_log=state_log,
    )


def _run_dirs(tmp_path: Path) -> "list[Path]":
    state_root = tmp_path / ".reyn" / "pipeline" / "state"
    if not state_root.is_dir():
        return []
    return [d for d in state_root.iterdir() if d.is_dir()]


def _result_json(run_dir: Path) -> "dict | None":
    p = run_dir / "result.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


async def _wait_for(pred, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


# A generated definition an agent might emit: transform -> tool -> agent, plus a
# named store — the whole point is that this is a STRING, parsed at call time.
_GOOD_DEF = """
pipeline: greet_and_write
description: a generated pipeline
steps:
  - transform: {value: "'hello ' + ctx.name", output: msg}
  - tool: {name: is4_write, args: {path: !expr ctx.out_path, content: !expr ctx.msg}}
  - agent: {prompt: "review {ctx.msg}"}
"""

# tool-only variant (no agent step) for the async path — the invoker's own
# scripted turn (consuming the pipeline_result) is then the ONLY LLM call.
_GOOD_DEF_TOOL_ONLY = """
pipeline: write_only
steps:
  - transform: {value: "'hi ' + ctx.name", output: msg}
  - tool: {name: is4_write, args: {path: !expr ctx.out_path, content: !expr ctx.msg}}
"""


# ── inline happy path: sync ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inline_sync_happy_path_runs_and_returns_inline(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a generated DSL string passes the gate and runs to completion in
    the attached driver-session — the tool step REALLY wrote the file, the final
    output is the (scripted) agent reply, and it is returned INLINE. RED if the
    inline entry stopped feeding the parsed Pipeline into the shared attached
    launch, or the gate wrongly rejected a valid definition."""
    _install_write_tool(monkeypatch)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("the final answer")
    reg = _agent_registry(tmp_path, state_log, scripted)
    caller = reg.get_or_load("worker")
    out_file = tmp_path / "out.txt"

    result = await _handle_run_pipeline_inline(
        {"definition": _GOOD_DEF, "input": {"name": "world", "out_path": str(out_file)}},
        _ctx(reg, caller, state_log),
    )

    assert result["status"] == "ok"
    assert result["data"]["output"] == "the final answer"
    assert result["data"]["named_stores"]["msg"] == "hello world"
    # Real tool side effect (not a stub); exactly the agent step called the LLM.
    assert out_file.read_text(encoding="utf-8").splitlines() == ["hello world"]
    assert scripted.calls == 1


# ── inline happy path: async ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inline_async_happy_path_launches_and_delivers(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: ``run_pipeline_inline_async`` returns {status: started, run_id}
    immediately (invocation.json on disk before completion), the driver-session
    runs the generated pipeline, and the result is delivered to the invoker's
    inbox (its scripted turn consumes it). RED if the async inline path stopped
    persisting the generated def / stopped delivering."""
    _install_write_tool(monkeypatch)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("acknowledged")
    reg = _agent_registry(tmp_path, state_log, scripted)
    caller = reg.get_or_load("worker")
    out_file = tmp_path / "out.txt"

    result = await _handle_run_pipeline_inline_async(
        {"definition": _GOOD_DEF_TOOL_ONLY,
         "input": {"name": "async", "out_path": str(out_file)}},
        _ctx(reg, caller, state_log),
    )
    assert result["status"] == "started"
    run_dir = pipeline_run_dir(tmp_path / ".reyn", result["data"]["run_id"])
    assert (run_dir / "invocation.json").is_file()

    assert await _wait_for(lambda: _result_json(run_dir) is not None)
    terminal = _result_json(run_dir)
    assert terminal["status"] == "ok" and terminal["delivered"] is True
    assert out_file.read_text(encoding="utf-8").splitlines() == ["hi async"]
    assert await _wait_for(lambda: scripted.calls >= 1)


# ── #2572: inline pipeline runtime verify:schema enforcement ────────────────
#
# ``_prepare_inline_launch`` parses a per-call SchemaRegistry from the
# definition's own ``schema:`` documents (IS-3) but, before this fix,
# discarded it instead of threading it to the launch — so ANY inline
# ``verify: schema`` step (declared alongside a ``schema:`` doc that the
# static gate itself requires resolve, check 2) crashed the driver-session
# with "no schema_registry was provided" REGARDLESS of whether the tool's
# result actually conformed. These tests prove the registry now reaches the
# executor: a conforming result runs to ``ok``; a violating one fails with a
# real schema-validation error.

_SCHEMA_OK_DEF = """
schema: Out
fields:
  content: {type: string, required: true}
---
pipeline: schema_ok
steps:
  - tool: {name: is4_write, args: {path: !expr ctx.out_path, content: "hello"}, schema: Out}
"""

_SCHEMA_BAD_DEF = """
schema: Out
fields:
  nonexistent_field: {type: string, required: true}
---
pipeline: schema_bad
steps:
  - tool: {name: is4_write, args: {path: !expr ctx.out_path, content: "hello"}, schema: Out}
"""


@pytest.mark.asyncio
async def test_inline_sync_enforces_verify_schema_pass_and_fail(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: an inline pipeline's ``verify: schema`` step is enforced in the
    sync-attached driver-session — a conforming tool result runs to ``ok``, a
    violating one fails with a real schema-validation error naming the
    schema (not the "no schema_registry" construction error the runtime used
    to raise unconditionally, since the per-call registry was parsed then
    discarded instead of threaded to the launch)."""
    _install_write_tool(monkeypatch)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log, None)
    caller = reg.get_or_load("worker")
    out_file = tmp_path / "out.txt"

    ok_result = await _handle_run_pipeline_inline(
        {"definition": _SCHEMA_OK_DEF, "input": {"out_path": str(out_file)}},
        _ctx(reg, caller, state_log),
    )
    assert ok_result["status"] == "ok"
    assert out_file.read_text(encoding="utf-8").splitlines() == ["hello"]

    bad_result = await _handle_run_pipeline_inline(
        {"definition": _SCHEMA_BAD_DEF, "input": {"out_path": str(out_file)}},
        _ctx(reg, caller, state_log),
    )
    assert bad_result["status"] == "error"
    assert "failed schema" in bad_result["data"]["error"]
    assert "no schema_registry was provided" not in bad_result["data"]["error"]


@pytest.mark.asyncio
async def test_inline_async_enforces_verify_schema_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the SAME enforcement on the async inline launch path — the
    terminal marker records ``failed`` with a real schema error."""
    _install_write_tool(monkeypatch)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("ack")
    reg = _agent_registry(tmp_path, state_log, scripted)
    caller = reg.get_or_load("worker")
    out_file = tmp_path / "out.txt"

    result = await _handle_run_pipeline_inline_async(
        {"definition": _SCHEMA_BAD_DEF, "input": {"out_path": str(out_file)}},
        _ctx(reg, caller, state_log),
    )
    assert result["status"] == "started"
    run_dir = pipeline_run_dir(tmp_path / ".reyn", result["data"]["run_id"])

    assert await _wait_for(lambda: _result_json(run_dir) is not None)
    terminal = _result_json(run_dir)
    assert terminal["status"] == "failed"
    assert "failed schema" in terminal["error"]
    assert "no schema_registry was provided" not in terminal["error"]


# ── static gate rejections — each fails, NOTHING spawned ─────────────────────


@pytest.mark.asyncio
async def test_gate_rejects_malformed_dsl_nothing_spawned(
    tmp_path: Path,
) -> None:
    """Tier 2: check 1 — a definition the parser cannot turn into exactly one
    pipeline (here: zero pipeline docs) is rejected with a clear error and
    spawns nothing."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log, None)
    caller = reg.get_or_load("worker")

    result = await _handle_run_pipeline_inline(
        {"definition": "schema: S\nfields: {a: {type: string}}"},
        _ctx(reg, caller, state_log),
    )
    assert result["status"] == "error"
    assert "invalid" in result["data"]["error"]
    assert _run_dirs(tmp_path) == []


@pytest.mark.asyncio
async def test_gate_rejects_unresolvable_schema_ref_nothing_spawned(
    tmp_path: Path,
) -> None:
    """Tier 2: check 2 — a step ``schema:`` REF not defined by any inline
    ``schema:`` document is rejected before spawn."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log, None)
    caller = reg.get_or_load("worker")
    definition = (
        "pipeline: p\n"
        "steps:\n"
        "  - tool: {name: web_search, args: {query: hi}, schema: NoSuchSchema}\n"
    )

    result = await _handle_run_pipeline_inline(
        {"definition": definition}, _ctx(reg, caller, state_log),
    )
    assert result["status"] == "error"
    assert "NoSuchSchema" in result["data"]["error"]
    assert _run_dirs(tmp_path) == []


@pytest.mark.asyncio
async def test_gate_rejects_unknown_tool_nothing_spawned(tmp_path: Path) -> None:
    """Tier 2: check 3 — a ``tool`` step naming a tool that resolves to no
    registered tool is rejected before spawn."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log, None)
    caller = reg.get_or_load("worker")
    definition = "pipeline: p\nsteps:\n  - tool: {name: does_not_exist_xyz, args: {}}\n"

    result = await _handle_run_pipeline_inline(
        {"definition": definition}, _ctx(reg, caller, state_log),
    )
    assert result["status"] == "error"
    assert "does_not_exist_xyz" in result["data"]["error"]
    assert _run_dirs(tmp_path) == []


@pytest.mark.asyncio
async def test_gate_rejects_s3_nested_launch_nothing_spawned(tmp_path: Path) -> None:
    """Tier 2: check 5 (R6 S3) — a ``tool`` step that would itself launch a
    pipeline (here the inline verb, closing the sibling loophole) is rejected
    statically before spawn (nesting is call-only)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log, None)
    caller = reg.get_or_load("worker")
    definition = "pipeline: p\nsteps:\n  - tool: {name: run_pipeline_inline, args: {}}\n"

    result = await _handle_run_pipeline_inline(
        {"definition": definition}, _ctx(reg, caller, state_log),
    )
    assert result["status"] == "error"
    assert "structurally denied" in result["data"]["error"]
    assert _run_dirs(tmp_path) == []


@pytest.mark.asyncio
async def test_gate_rejects_non_invoker_agent_identity_nothing_spawned(
    tmp_path: Path,
) -> None:
    """Tier 2: check 6 (INLINE-ONLY, escalation prevention) — a generated
    pipeline whose ``agent`` step declares an identity OTHER than the invoker
    would run under that agent's (possibly larger) envelope. The gate rejects it
    before spawn; a step with ``identity`` unset (inherit invoker) or naming the
    invoker is allowed. RED if the gate let a non-invoker identity through — a
    real capability-escalation hole."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log, None)
    if not reg.exists("privileged_agent"):
        reg.create("privileged_agent")
    caller = reg.get_or_load("worker")  # invoker = "worker"
    definition = (
        "pipeline: p\n"
        "steps:\n"
        "  - agent: {prompt: do it, identity: privileged_agent}\n"
    )

    result = await _handle_run_pipeline_inline(
        {"definition": definition}, _ctx(reg, caller, state_log),
    )
    assert result["status"] == "error"
    assert "privileged_agent" in result["data"]["error"]
    assert "escalation" in result["data"]["error"]
    assert _run_dirs(tmp_path) == []


@pytest.mark.asyncio
async def test_gate_allows_invoker_identity_and_unset(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: check 6 positive control — an agent step with ``identity`` unset
    (inherit invoker) OR explicitly the invoker's own name PASSES the gate and
    runs (so the rejection above is not vacuous — the check keys on identity,
    not on the mere presence of an agent step)."""
    _install_write_tool(monkeypatch)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("ok")
    reg = _agent_registry(tmp_path, state_log, scripted)
    caller = reg.get_or_load("worker")
    definition = (
        "pipeline: p\n"
        "steps:\n"
        "  - agent: {prompt: unset identity}\n"
        "  - agent: {prompt: explicit invoker, identity: worker}\n"
    )

    result = await _handle_run_pipeline_inline(
        {"definition": definition}, _ctx(reg, caller, state_log),
    )
    assert result["status"] == "ok"


# ── inline arg / wiring error contracts ──────────────────────────────────────


@pytest.mark.asyncio
async def test_inline_missing_definition_and_wiring_errors(tmp_path: Path) -> None:
    """Tier 1: a missing ``definition`` and a non-wired context each fail
    clearly (never a silent no-op launch), and spawn nothing."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log, None)
    caller = reg.get_or_load("worker")

    empty = await _handle_run_pipeline_inline({}, _ctx(reg, caller, state_log))
    assert empty["status"] == "error" and "definition" in empty["data"]["error"]

    no_wire = await _handle_run_pipeline_inline(
        {"definition": "pipeline: p\nsteps:\n  - transform: {value: \"1\"}\n"},
        _ctx(reg, caller, state_log, wired=False),
    )
    assert no_wire["status"] == "error"
    assert "router context" in no_wire["data"]["error"]
    assert _run_dirs(tmp_path) == []


# ── S3 sibling sweep: inline verbs are non-grantable inside a pipeline ───────


@pytest.mark.asyncio
async def test_tool_step_dispatch_denies_inline_launch() -> None:
    """Tier 2b: the SHARED tool-step dispatch (both the sync tool path and the
    driver path build it here) structurally denies the inline launch verbs — a
    ToolStep may not launch an inline pipeline (bare AND qualified names)."""
    from reyn.core.pipeline.executor import PipelineExecutionError

    dispatch = _make_tool_dispatch(_bare_ctx())
    for denied in ("run_pipeline_inline", "run_pipeline_inline_async",
                   "pipeline__run_inline", "pipeline__run_inline_async"):
        with pytest.raises(PipelineExecutionError) as exc:
            await dispatch(denied, {})
        assert "structurally denied" in str(exc.value)


def test_agent_step_narrowing_denies_inline_launch() -> None:
    """Tier 2b: OS invariant (R6 S3 sibling sweep) — the agent-step spawn
    narrowing denies the inline launch verbs alongside the registered ones, so
    an agent step cannot re-open the inline escape hatch."""
    from reyn.runtime.session_api import _build_agent_step_narrowing

    deny = _build_agent_step_narrowing(["read_file"])["tool_deny"]
    assert "run_pipeline_inline" in deny
    assert "run_pipeline_inline_async" in deny


# ── inline crash-resume e2e (truncate-falsify: generated def survives) ───────


@pytest.mark.asyncio
async def test_inline_run_crash_resumes_exactly_once_after_wal_truncation(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: an INLINE run's recovery source is the GENERATED definition
    serialized into invocation.json — not a registry entry. A run whose steps
    0-1 completed (R4 gens on disk) crashes before the terminal marker; the WAL
    is then truncated below the crash-state's source events. ``restore_all``
    re-creates the driver-session from invocation.json alone and resumes
    EXACTLY-ONCE (steps 0-1 replay from the gen FILE, only step 2 executes),
    then delivers. RED if the inline recovery source rode a truncatable WAL
    event, or if resume re-ran a completed step."""
    _install_write_tool(monkeypatch)
    wal_path = tmp_path / ".reyn" / "wal.jsonl"
    state_log = StateLog(wal_path)
    out_file = tmp_path / "out.txt"
    reg = _agent_registry(tmp_path, state_log, None)  # creates agent "worker"

    # A three-tool-step generated definition (deterministic, no agent step so the
    # exactly-once file assertion is unambiguous).
    definition = (
        "pipeline: gen\n"
        "steps:\n"
        f"  - tool: {{name: is4_write, args: {{path: {json.dumps(str(out_file))}, content: a}}}}\n"
        f"  - tool: {{name: is4_write, args: {{path: {json.dumps(str(out_file))}, content: b}}}}\n"
        f"  - tool: {{name: is4_write, args: {{path: {json.dumps(str(out_file))}, content: c}}}}\n"
    )
    full = parse_pipeline_dsl(definition, SchemaRegistry())

    # Crash state: run a 2-step prefix (steps 0-1 → gens on disk), then persist
    # the FULL generated pipeline as the work-order (exactly what
    # start_pipeline_run serializes via pipeline_to_dict — a generated Pipeline,
    # no registry name).
    from reyn.core.pipeline.executor import Pipeline as _Pipeline
    prefix = _Pipeline(steps=list(full.steps[:2]))
    await PipelineExecutor().run(
        prefix, None, tool_dispatch=_make_tool_dispatch(_bare_ctx(state_log)),
        state_log=state_log, run_id="run-inline",
    )
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["a", "b"]

    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-inline")
    write_invocation(run_dir, PipelineWorkOrder(
        run_id="run-inline", pipeline_name="inline",
        pipeline=pipeline_to_dict(full), input=None,
        reply_to_agent="worker", reply_to_sid="main",
        driver_agent="worker", driver_sid="drv-inline", spawn_seq=1,
    ))

    # Advance + truncate the WAL below the crash-state's source seqs (incl.
    # spawn_seq=1) — the serialized generated def must survive.
    for i in range(50):
        await state_log.append("inbox_put", n=100 + i)
    await state_log.truncate_below(40)
    await state_log.flush()
    assert all(e["seq"] >= 40 for e in state_log.iter_from(0))

    # Restart + recover.
    state_log2 = StateLog(wal_path)
    scripted = _ScriptedAgentReply("resumed")
    reg2 = _agent_registry(tmp_path, state_log2, scripted)
    await reg2.restore_all()

    assert await _wait_for(lambda: _result_json(run_dir) is not None)
    terminal = _result_json(run_dir)
    assert terminal["status"] == "ok" and terminal["delivered"] is True
    # Exactly-once: steps 0-1 replayed (no duplicate a/b), only step 2 executed.
    assert out_file.read_text(encoding="utf-8").splitlines() == ["a", "b", "c"]
    assert await _wait_for(lambda: scripted.calls >= 1)
