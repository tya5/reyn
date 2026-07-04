"""Tier 2: IS-2 async pipeline driver-session — launch, crash-resume, recovery gates.

Covers the D案 architecture end-to-end with real collaborators (real
``AgentRegistry``/``Session``/``StateLog``/``PipelineExecutor``; the only fake
is the scripted LLM callable injected through the real ``RouterLoopDriver``
``_loop_observer`` seam, the same discipline as
``test_run_pipeline_tool_is1.py``):

- ``run_pipeline_async`` returns ``{status: started, run_id}`` immediately;
  the run completes in its driver-session and the result arrives on the
  invoker's inbox as a ``pipeline_result`` turn (scripted LLM consumed it).
- the CLAUDE.md recovery gate: truncate-falsify (invocation.json + R4 gens
  survive WAL truncation below their source events; recovery resumes
  EXACTLY-ONCE — completed steps replay, only the remaining step executes).
- crash windows: before the first R4 gen (original input preserved — resume's
  ``initial_context=None`` fallback must not be reached), before the first
  session snapshot (the scan re-CREATES the driver-session from
  invocation.json alone), and between last step and delivery (steps-complete
  but unmarked run re-delivers without re-executing).
- A8 poison cap (attempts past the cap → terminal failure, not a crash-loop),
- A9 rewind-guard polarity (abandoned-branch spawn_seq skipped; the truncated
  case re-waking is proven by the truncate-falsify test),
- R6 S3 structural deny for pipeline tool steps (launch/delegate tools).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import (
    AgentStep,
    ExprRef,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.core.pipeline.serde import (
    PipelineSerdeError,
    pipeline_from_dict,
    pipeline_to_dict,
)
from reyn.core.pipeline.work_order import (
    PipelineWorkOrder,
    bump_resume_attempts,
    pipeline_run_dir,
    read_resume_attempts,
    write_invocation,
)
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.tools.pipeline_verbs import (
    _handle_run_pipeline,
    _handle_run_pipeline_async,
    _make_tool_dispatch,
)
from reyn.tools.types import RouterCallerState, ToolContext


class _ScriptedAgentReply:
    """Always answers with one fixed plain-text turn — the LLM is incidental
    to what's under test (same rationale as test_run_pipeline_tool_is1.py)."""

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
    """Real AgentRegistry + real Session factory (mirrors the IS-1 test)."""
    holder: dict = {}

    def _factory(profile) -> Session:
        s = Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
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


def _install_side_effect_tool(monkeypatch) -> None:
    """Make a REAL side-effecting tool resolvable through the production
    dispatch (appends a line to a file AND a WAL entry via ctx.state_log — so
    R4 gens land at distinct seqs, same shape as test_pipeline_executor_r3_r4's
    counting dispatch). ``get_default_registry`` builds a FRESH registry per
    call, so the tool is added by wrapping the real builder with a real
    callable (the policy-allowed monkeypatch idiom — every lookup still goes
    through the real ``ToolRegistry.register``/``lookup`` contract)."""
    import reyn.tools as tools_pkg
    from reyn.tools.types import ToolDefinition, ToolGates

    async def _handler(args, ctx):
        p = Path(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(str(args["line"]) + "\n")
        if ctx.state_log is not None:
            await ctx.state_log.append("inbox_put", note="is2-side-effect")
        return {"line": str(args["line"])}

    tool = ToolDefinition(
        name="is2_append",
        description="IS-2 test: append a line to a file (real side effect).",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow", phase="allow"),
        handler=_handler,
        category="io",
        purity="side_effect",
    )
    base_build = tools_pkg.get_default_registry

    def _with_tool():
        registry = base_build()
        registry.register(tool)
        return registry

    monkeypatch.setattr(tools_pkg, "get_default_registry", _with_tool)


def _bare_ctx(state_log: "StateLog | None" = None) -> ToolContext:
    from reyn.core.events.events import EventLog
    return ToolContext(
        events=EventLog(), permission_resolver=None, workspace=None,
        caller_kind="router", router_state=None, state_log=state_log,
    )


def _three_step_pipeline(out_file: Path) -> Pipeline:
    return Pipeline(
        steps=[
            TransformStep(value="ctx.seed + 1", output="t0"),
            ToolStep(
                name="is2_append",
                args={"path": str(out_file), "line": ExprRef("ctx.t0")},
                output="t1",
            ),
            ToolStep(
                name="is2_append",
                args={"path": str(out_file), "line": "second"},
                output="t2",
            ),
        ],
        description="IS-2 test pipeline",
    )


async def _wait_for(pred, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def _result_json(run_dir: Path) -> "dict | None":
    p = run_dir / "result.json"
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _crash_state_work_order(
    run_id: str, pipeline: Pipeline, *, input: "dict | None",
    driver_sid: str = "drv1", spawn_seq: "int | None" = None,
    schema_defs: "dict | None" = None,
) -> PipelineWorkOrder:
    return PipelineWorkOrder(
        run_id=run_id,
        pipeline_name="p",
        pipeline=pipeline_to_dict(pipeline),
        input=input,
        reply_to_agent="worker",
        reply_to_sid="main",
        driver_agent="worker",
        driver_sid=driver_sid,
        spawn_seq=spawn_seq,
        schema_defs=schema_defs,
    )


# ── serde round-trip + kind-marker collision ────────────────────────────────


def test_serde_round_trip_non_default_values() -> None:
    """Tier 1: Pipeline ⇄ dict ⇄ JSON round-trips every step kind with
    NON-DEFAULT field values (ExprRef args, schema, capabilities, output,
    description) — the work-order persistence contract."""
    pipeline = Pipeline(
        steps=[
            TransformStep(value="ctx.a + 1", output="t"),
            ToolStep(
                name="is2_append",
                args={"path": "/tmp/x", "line": ExprRef("ctx.t"), "n": 3,
                      "nested": {"deep": [1, "two"]}},
                output="w",
                schema="OutShape",
            ),
            AgentStep(
                prompt="summarize {ctx.w}",
                identity="worker",
                capabilities=["read_file"],
                schema="Reply",
                output="summary",
            ),
        ],
        description="round-trip pipeline",
    )
    wire = json.loads(json.dumps(pipeline_to_dict(pipeline)))
    assert pipeline_from_dict(wire) == pipeline


def test_serde_exprref_marker_collision_is_refused_both_directions() -> None:
    """Tier 1: the ``__exprref__`` kind-marker cannot be spoofed. Encode: a
    LITERAL arg dict carrying the marker key is a hard error naming the arg
    (not a silent mis-round-trip). Decode: only the exact one-key marker shape
    decodes to ExprRef — a marker-key dict with extra keys stays a literal."""
    colliding = Pipeline(steps=[
        ToolStep(name="t", args={"payload": {"__exprref__": "ctx.x"}}),
    ])
    with pytest.raises(PipelineSerdeError) as exc:
        pipeline_to_dict(colliding)
    assert "payload" in str(exc.value) and "__exprref__" in str(exc.value)

    # Decode direction: extra keys → NOT an ExprRef (stays a literal dict).
    wire = {
        "description": "",
        "steps": [{
            "kind": "tool", "name": "t",
            "args": {"payload": {"__exprref__": "ctx.x", "other": 1}},
            "output": None, "schema": None,
        }],
    }
    decoded = pipeline_from_dict(wire)
    assert decoded.steps[0].args["payload"] == {"__exprref__": "ctx.x", "other": 1}
    # ...and the exact marker shape DOES decode to an ExprRef.
    wire["steps"][0]["args"] = {"payload": {"__exprref__": "ctx.x"}}
    assert pipeline_from_dict(wire).steps[0].args["payload"] == ExprRef("ctx.x")


# ── R6 S3 structural deny for tool steps (sync + async share the dispatch) ──


@pytest.mark.asyncio
async def test_tool_step_dispatch_structurally_denies_launch_and_delegation() -> None:
    """Tier 2b: a pipeline ToolStep must not launch a pipeline (sync OR async)
    or delegate — R6 S3 (nesting is call-only). Checked through the SHARED
    ``_make_tool_dispatch`` (both the sync run_pipeline path and the IS-2
    driver path build their dispatch here), for bare AND qualified names."""
    dispatch = _make_tool_dispatch(_bare_ctx())
    for denied in ("run_pipeline", "run_pipeline_async", "delegate_to_agent",
                   "pipeline__run", "multi_agent__delegate"):
        with pytest.raises(PipelineExecutionError) as exc:
            await dispatch(denied, {})
        assert "structurally denied" in str(exc.value)


def test_agent_step_narrowing_denies_async_launch() -> None:
    """Tier 2b: the agent-step spawn narrowing (R5/R6 S3) denies the ASYNC
    launch alongside the sync one — the sibling escape hatch is closed."""
    from reyn.runtime.session_api import _build_agent_step_narrowing
    deny = _build_agent_step_narrowing(["read_file"])["tool_deny"]
    assert "run_pipeline_async" in deny and "run_pipeline" in deny


# ── async launch e2e: tool → driver-session → pipeline_result ───────────────


@pytest.mark.asyncio
async def test_run_pipeline_async_launches_and_delivers_result(tmp_path: Path, monkeypatch) -> None:
    """Tier 2c: ``run_pipeline_async`` returns {status: started, run_id}
    IMMEDIATELY; the driver-session runs the pipeline to completion (real tool
    side effect), posts the result to the invoker (whose scripted-LLM turn
    consumes it), writes the terminal marker AFTER delivery, and the
    driver-session is reclaimed (A10 — no leak past terminal)."""
    _install_side_effect_tool(monkeypatch)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("acknowledged")
    reg = _agent_registry(tmp_path, state_log, scripted)
    out_file = tmp_path / "out.txt"

    pipeline_registry = PipelineRegistry()
    pipeline_registry.register("p", _three_step_pipeline(out_file))

    caller = reg.get_or_load("worker")
    ctx = ToolContext(
        events=caller._router_host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            pipeline_registry=pipeline_registry,
            agent_registry=reg,
            host=caller._router_host,
        ),
        state_log=state_log,
    )

    result = await _handle_run_pipeline_async({"name": "p", "input": {"seed": 10}}, ctx)
    assert result["status"] == "started"
    run_id = result["data"]["run_id"]
    run_dir = pipeline_run_dir(tmp_path / ".reyn", run_id)
    # invocation.json is on disk BEFORE completion (work-order-at-birth).
    assert (run_dir / "invocation.json").is_file()

    assert await _wait_for(lambda: _result_json(run_dir) is not None)
    terminal = _result_json(run_dir)
    assert terminal["status"] == "ok" and terminal["delivered"] is True
    # Real tool side effects: transform seeded 10 → 11, then the fixed line.
    assert out_file.read_text(encoding="utf-8").splitlines() == ["11", "second"]
    # The invoker actually CONSUMED the pipeline_result (a scripted-LLM turn ran).
    assert await _wait_for(lambda: scripted.calls >= 1)
    # A10: the driver-session vanishes after terminal.
    invocation = json.loads((run_dir / "invocation.json").read_text(encoding="utf-8"))
    assert await _wait_for(
        lambda: reg.get_session("worker", invocation["driver_sid"]) is None
    )


@pytest.mark.asyncio
async def test_run_pipeline_async_error_contracts(tmp_path: Path) -> None:
    """Tier 1: unregistered pipeline name and missing WAL each yield a clear
    error (never a silent no-op launch)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log, None)
    caller = reg.get_or_load("worker")
    registry = PipelineRegistry()

    def _ctx(sl):
        return ToolContext(
            events=caller._router_host.events, permission_resolver=None,
            workspace=None, caller_kind="router",
            router_state=RouterCallerState(
                pipeline_registry=registry, agent_registry=reg,
                host=caller._router_host,
            ),
            state_log=sl,
        )

    missing = await _handle_run_pipeline_async({"name": "nope"}, _ctx(state_log))
    assert missing["status"] == "error"
    assert "not registered" in missing["data"]["error"]

    registry.register("p", Pipeline(steps=[TransformStep(value="1")]))
    no_wal = await _handle_run_pipeline_async({"name": "p"}, _ctx(None))
    assert no_wal["status"] == "error"
    assert "state_log" in no_wal["data"]["error"]


# ── #2572: registered pipeline runtime verify:schema enforcement ───────────
#
# Before this fix the driver-session's ``executor.run``/``resume`` never
# received a ``schema_registry`` (regardless of what the caller registered
# it with), so ANY ``verify: schema`` step — pass or fail — crashed with
# "declares verify: schema ... but no schema_registry was provided". The
# tests below prove the registry now actually reaches the executor: a
# CONFORMING result runs to ``ok``, and a VIOLATING result fails with a real
# schema-validation error (not the old "no schema_registry" construction
# error).


def _line_schema_registry(*, required_field: str) -> SchemaRegistry:
    """A one-field schema registry naming the schema "LineOut". Passing
    ``required_field="line"`` matches the ``is2_append`` tool's real return
    shape (``{"line": <str>}``) — conforming; any other name makes every
    result violate it (the required field is always absent) — a controlled
    non-conforming case that never depends on value content."""
    schema_registry = SchemaRegistry()
    schema_registry.register(
        "LineOut", {"fields": {required_field: {"type": "string", "required": True}}},
    )
    return schema_registry


def _schema_checked_pipeline(out_file: Path, *, line: str) -> Pipeline:
    return Pipeline(steps=[
        ToolStep(
            name="is2_append", args={"path": str(out_file), "line": line},
            output="t", schema="LineOut",
        ),
    ])


@pytest.mark.asyncio
async def test_registered_pipeline_enforces_verify_schema_sync_attached(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2c: a REGISTERED pipeline's ``verify: schema`` step is enforced in
    the sync-attached driver-session: a conforming tool result runs to
    ``ok``, a violating one fails with a real schema-validation error naming
    the schema (not the "no schema_registry" construction error the runtime
    used to raise unconditionally, since it never threaded a registry)."""
    _install_side_effect_tool(monkeypatch)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _agent_registry(tmp_path, state_log, None)
    caller = reg.get_or_load("worker")
    out_file = tmp_path / "out.txt"

    pipeline_registry = PipelineRegistry()
    pipeline_registry.register(
        "ok", _schema_checked_pipeline(out_file, line="10"),
        _line_schema_registry(required_field="line"),
    )
    pipeline_registry.register(
        "bad", _schema_checked_pipeline(out_file, line="11"),
        _line_schema_registry(required_field="nonexistent_field"),
    )

    def _ctx() -> ToolContext:
        return ToolContext(
            events=caller._router_host.events, permission_resolver=None,
            workspace=None, caller_kind="router",
            router_state=RouterCallerState(
                pipeline_registry=pipeline_registry, agent_registry=reg,
                host=caller._router_host,
            ),
            state_log=state_log,
        )

    ok_result = await _handle_run_pipeline({"name": "ok"}, _ctx())
    assert ok_result["status"] == "ok"

    bad_result = await _handle_run_pipeline({"name": "bad"}, _ctx())
    assert bad_result["status"] == "error"
    assert "failed schema" in bad_result["data"]["error"]
    assert "no schema_registry was provided" not in bad_result["data"]["error"]


@pytest.mark.asyncio
async def test_registered_pipeline_enforces_verify_schema_async(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2c: the SAME enforcement on the async launch path
    (``run_pipeline_async``) — the terminal marker records ``failed`` with a
    real schema error for a violating result, ``ok`` for a conforming one."""
    _install_side_effect_tool(monkeypatch)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    scripted = _ScriptedAgentReply("ack")
    reg = _agent_registry(tmp_path, state_log, scripted)
    caller = reg.get_or_load("worker")
    out_file = tmp_path / "out.txt"

    pipeline_registry = PipelineRegistry()
    pipeline_registry.register(
        "bad-async", _schema_checked_pipeline(out_file, line="20"),
        _line_schema_registry(required_field="nonexistent_field"),
    )

    ctx = ToolContext(
        events=caller._router_host.events, permission_resolver=None,
        workspace=None, caller_kind="router",
        router_state=RouterCallerState(
            pipeline_registry=pipeline_registry, agent_registry=reg,
            host=caller._router_host,
        ),
        state_log=state_log,
    )

    result = await _handle_run_pipeline_async({"name": "bad-async"}, ctx)
    assert result["status"] == "started"
    run_id = result["data"]["run_id"]
    run_dir = pipeline_run_dir(tmp_path / ".reyn", run_id)

    assert await _wait_for(lambda: _result_json(run_dir) is not None)
    terminal = _result_json(run_dir)
    assert terminal["status"] == "failed"
    assert "failed schema" in terminal["error"]
    assert "no schema_registry was provided" not in terminal["error"]


# ── recovery: the CLAUDE.md truncate-falsify gate + kill/restore/resume ─────


@pytest.mark.asyncio
async def test_truncate_falsify_recovery_source_survives_wal_truncation(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: MANDATORY CLAUDE.md recovery gate + the kill→restore→resume
    e2e. A run crashes mid-pipeline (steps 0-1 done, R4 gens + invocation.json
    on disk; the driver-session died BEFORE any session snapshot — the
    binding-(3) zero-snapshot window; its spawn WAL record is then TRUNCATED
    away). ``restore_all`` must still re-CREATE the driver-session from
    invocation.json alone, resume EXACTLY-ONCE (steps 0-1 replayed from the
    gen FILE, only step 2 executes a new tool call), and deliver the result.
    RED if any recovery source rode a truncatable WAL event, or if the rewind
    guard had the wrong (default-closed) polarity — the truncated spawn_seq
    must NOT block the re-wake."""
    _install_side_effect_tool(monkeypatch)
    wal_path = tmp_path / ".reyn" / "wal.jsonl"
    state_log = StateLog(wal_path)
    out_file = tmp_path / "out.txt"
    full = _three_step_pipeline(out_file)
    reg = _agent_registry(tmp_path, state_log, None)  # creates agent "worker"

    # Crash state: the first TWO steps completed (run a 2-step prefix — the
    # same phase-1 idiom as test_pipeline_executor_r3_r4), gens recorded at
    # real WAL seqs (the side-effect tool appends an entry per call).
    prefix = Pipeline(steps=list(full.steps[:2]))
    await PipelineExecutor().run(
        prefix, {"seed": 10},
        tool_dispatch=_make_tool_dispatch(_bare_ctx(state_log)),
        state_log=state_log, run_id="run-t",
    )
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["11"]

    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-t")
    write_invocation(run_dir, _crash_state_work_order(
        "run-t", full, input={"seed": 10}, spawn_seq=1,
    ))

    # Other activity advances the WAL head; GC truncates below 40 — the
    # crash-state's source seqs (incl. spawn_seq=1) are GONE from the WAL.
    for i in range(50):
        await state_log.append("inbox_put", n=100 + i)
    await state_log.truncate_below(40)
    await state_log.flush()
    assert all(e["seq"] >= 40 for e in state_log.iter_from(0))

    # ── restart: fresh StateLog + fresh registry, then recovery. ──
    state_log2 = StateLog(wal_path)
    scripted = _ScriptedAgentReply("resumed ok")
    reg2 = _agent_registry(tmp_path, state_log2, scripted)
    await reg2.restore_all()

    assert await _wait_for(lambda: _result_json(run_dir) is not None)
    terminal = _result_json(run_dir)
    assert terminal["status"] == "ok" and terminal["delivered"] is True
    # Exactly-once: steps 0-1 replayed (no new "11" line), only step 2 ran.
    assert out_file.read_text(encoding="utf-8").splitlines() == ["11", "second"]
    # The invoker consumed the re-delivered result.
    assert await _wait_for(lambda: scripted.calls >= 1)


@pytest.mark.asyncio
async def test_truncate_falsify_schema_defs_survives_wal_truncation(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: CLAUDE.md recovery gate for #2572's ``schema_defs`` specifically
    — the run's SchemaRegistry must survive WAL truncation via
    ``invocation.json`` ALONE, not a WAL event, same as the pipeline
    definition itself. Steps 0-1 (no schema) run to completion pre-crash;
    step 2 declares ``verify: schema`` against a schema that step's real tool
    result does NOT conform to. The WAL is truncated below the crash-state's
    source seqs (incl. spawn_seq), then a FRESH StateLog + registry recovers.
    RED if ``schema_defs`` rode a truncatable WAL event (the resumed step
    would either crash with "no schema_registry" instead of a real schema
    failure, or the recovery source would be gone entirely) — the resumed
    run must reach a real, enforced schema failure on step 2."""
    _install_side_effect_tool(monkeypatch)
    wal_path = tmp_path / ".reyn" / "wal.jsonl"
    state_log = StateLog(wal_path)
    out_file = tmp_path / "out.txt"
    # step 2 (the one NOT yet run at crash time) declares a schema the real
    # is2_append return ({"line": ...}) does not conform to (required field
    # "nonexistent_field" is never present) — a controlled, guaranteed failure
    # that proves the schema was actually reached and enforced, not skipped.
    full = Pipeline(steps=[
        *_three_step_pipeline(out_file).steps[:2],
        ToolStep(
            name="is2_append", args={"path": str(out_file), "line": "second"},
            output="t2", schema="LineOut",
        ),
    ])
    schema_defs = _line_schema_registry(required_field="nonexistent_field").as_dict()
    reg = _agent_registry(tmp_path, state_log, None)  # creates agent "worker"

    # Crash state: the first TWO steps completed (schema-free), gens recorded
    # at real WAL seqs.
    prefix = Pipeline(steps=list(full.steps[:2]))
    await PipelineExecutor().run(
        prefix, {"seed": 10},
        tool_dispatch=_make_tool_dispatch(_bare_ctx(state_log)),
        state_log=state_log, run_id="run-schema-t",
    )
    await state_log.flush()
    assert out_file.read_text(encoding="utf-8").splitlines() == ["11"]

    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-schema-t")
    write_invocation(run_dir, _crash_state_work_order(
        "run-schema-t", full, input={"seed": 10}, spawn_seq=1,
        schema_defs=schema_defs,
    ))

    # Other activity advances the WAL head; truncate below 40 — the crash
    # state's source seqs (incl. spawn_seq=1) are GONE from the WAL. schema_defs
    # lives only in invocation.json, never in a WAL record, so it is untouched.
    for i in range(50):
        await state_log.append("inbox_put", n=100 + i)
    await state_log.truncate_below(40)
    await state_log.flush()
    assert all(e["seq"] >= 40 for e in state_log.iter_from(0))

    # ── restart: fresh StateLog + fresh registry, then recovery. ──
    state_log2 = StateLog(wal_path)
    scripted = _ScriptedAgentReply("resumed")
    reg2 = _agent_registry(tmp_path, state_log2, scripted)
    await reg2.restore_all()

    assert await _wait_for(lambda: _result_json(run_dir) is not None)
    terminal = _result_json(run_dir)
    # The resumed step 2 hit a REAL schema violation (schema_defs survived
    # truncation intact) — not "no schema_registry was provided", which is
    # what would happen if the recovery path lost/never rebuilt the registry.
    assert terminal["status"] == "failed"
    assert "failed schema" in terminal["error"]
    assert "no schema_registry was provided" not in terminal["error"]
    # Exactly-once on the schema-free prefix (no new "11" line from a re-run);
    # step 2's tool call DOES execute (the schema check runs on its result,
    # AFTER the real side effect — same order as a live, non-recovered run).
    assert out_file.read_text(encoding="utf-8").splitlines() == ["11", "second"]
    assert await _wait_for(lambda: scripted.calls >= 1)


@pytest.mark.asyncio
async def test_recovery_before_first_gen_preserves_original_input(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the binding-(1) crash window — invocation.json written, NO R4
    gen yet (crash before step 0 completed), no driver-session record. Recovery
    must run the pipeline with the ORIGINAL work-order input (10 → the tool
    writes '11'), not resume's hardcoded initial_context=None fallback."""
    _install_side_effect_tool(monkeypatch)
    wal_path = tmp_path / ".reyn" / "wal.jsonl"
    state_log = StateLog(wal_path)
    out_file = tmp_path / "out.txt"
    full = _three_step_pipeline(out_file)
    reg = _agent_registry(tmp_path, state_log, None)
    assert reg.exists("worker")

    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-fresh")
    write_invocation(run_dir, _crash_state_work_order(
        "run-fresh", full, input={"seed": 10},
    ))

    state_log2 = StateLog(wal_path)
    scripted = _ScriptedAgentReply("ok")
    reg2 = _agent_registry(tmp_path, state_log2, scripted)
    await reg2.restore_all()

    assert await _wait_for(lambda: _result_json(run_dir) is not None)
    assert _result_json(run_dir)["status"] == "ok"
    assert out_file.read_text(encoding="utf-8").splitlines() == ["11", "second"]


@pytest.mark.asyncio
async def test_recovery_steps_done_but_undelivered_redelivers_without_rerun(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: A6 — terminal means RESULT DELIVERED, not steps-done. A run
    whose steps ALL completed but crashed before delivery (gens complete, no
    result.json) is re-woken; the driver replays every step from the snapshot
    (zero new tool calls) and delivers the result. RED if the recovery scan
    treated steps-done as terminal (the result would be lost forever)."""
    _install_side_effect_tool(monkeypatch)
    wal_path = tmp_path / ".reyn" / "wal.jsonl"
    state_log = StateLog(wal_path)
    out_file = tmp_path / "out.txt"
    full = _three_step_pipeline(out_file)
    reg = _agent_registry(tmp_path, state_log, None)
    assert reg.exists("worker")

    await PipelineExecutor().run(
        full, {"seed": 10},
        tool_dispatch=_make_tool_dispatch(_bare_ctx(state_log)),
        state_log=state_log, run_id="run-done",
    )
    await state_log.flush()
    lines_before = out_file.read_text(encoding="utf-8").splitlines()
    assert lines_before == ["11", "second"]

    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-done")
    write_invocation(run_dir, _crash_state_work_order(
        "run-done", full, input={"seed": 10},
    ))

    state_log2 = StateLog(wal_path)
    scripted = _ScriptedAgentReply("ok")
    reg2 = _agent_registry(tmp_path, state_log2, scripted)
    await reg2.restore_all()

    assert await _wait_for(lambda: _result_json(run_dir) is not None)
    terminal = _result_json(run_dir)
    assert terminal["status"] == "ok" and terminal["delivered"] is True
    # Exactly-once: nothing re-ran.
    assert out_file.read_text(encoding="utf-8").splitlines() == lines_before
    assert await _wait_for(lambda: scripted.calls >= 1)


@pytest.mark.asyncio
async def test_poison_run_past_resume_cap_fails_terminally(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: A8 — a run whose resume keeps crashing must not amplify a
    restart crash-loop. With the persisted attempt counter already at the cap,
    the next recovery re-wake terminal-FAILS the run (failure result
    delivered, marker written, NO step executed) instead of resuming."""
    _install_side_effect_tool(monkeypatch)
    wal_path = tmp_path / ".reyn" / "wal.jsonl"
    state_log = StateLog(wal_path)
    out_file = tmp_path / "out.txt"
    full = _three_step_pipeline(out_file)
    reg = _agent_registry(tmp_path, state_log, None)
    assert reg.exists("worker")

    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-poison")
    write_invocation(run_dir, _crash_state_work_order(
        "run-poison", full, input={"seed": 10},
    ))
    from reyn.runtime.services.pipeline_executor_driver import MAX_RESUME_ATTEMPTS
    for _ in range(MAX_RESUME_ATTEMPTS):
        bump_resume_attempts(run_dir)

    state_log2 = StateLog(wal_path)
    scripted = _ScriptedAgentReply("ok")
    reg2 = _agent_registry(tmp_path, state_log2, scripted)
    await reg2.restore_all()

    assert await _wait_for(lambda: _result_json(run_dir) is not None)
    terminal = _result_json(run_dir)
    assert terminal["status"] == "failed"
    assert "resume" in terminal["error"]
    assert not out_file.exists(), "no step may execute past the poison cap"
    assert await _wait_for(lambda: scripted.calls >= 1)  # failure IS delivered


@pytest.mark.asyncio
async def test_rewound_away_run_is_not_resurrected(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: A9 guard, abandoned-branch side — a run whose recorded
    spawn_seq is PROVABLY on an abandoned WAL branch (a rewind to before the
    spawn) is skipped by the recovery scan: no driver-session, no attempt
    bump, no result. (The DEFAULT-OPEN side — spawn record truncated away →
    still re-woken — is proven by the truncate-falsify test above.)"""
    _install_side_effect_tool(monkeypatch)
    wal_path = tmp_path / ".reyn" / "wal.jsonl"
    state_log = StateLog(wal_path)
    out_file = tmp_path / "out.txt"
    full = _three_step_pipeline(out_file)
    reg = _agent_registry(tmp_path, state_log, None)
    assert reg.exists("worker")

    # Advance the WAL past the spawn point, then rewind to BEFORE it — the
    # real reset-record shape (snapshot_generations.REWIND_KIND semantics).
    for i in range(6):
        await state_log.append("inbox_put", n=i)
    run_dir = pipeline_run_dir(tmp_path / ".reyn", "run-rewound")
    write_invocation(run_dir, _crash_state_work_order(
        "run-rewound", full, input={"seed": 10}, spawn_seq=5,
    ))
    from reyn.core.events.snapshot_generations import rewind
    await state_log.flush()
    await rewind(state_log, target_n=3)
    await state_log.flush()

    state_log2 = StateLog(wal_path)
    reg2 = _agent_registry(tmp_path, state_log2, None)
    rewoken = await reg2._rewake_pipeline_runs()

    assert rewoken == []
    assert _result_json(run_dir) is None
    assert read_resume_attempts(run_dir) == 0
    assert reg2.get_session("worker", "drv1") is None
