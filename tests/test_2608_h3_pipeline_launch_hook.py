"""Tests for #2608 H3 — the ``pipeline_launch`` hook action.

H3 adds the 4th hook action (alongside ``template_push`` / ``shell_exec`` /
``shell_push``): a hook can launch a REGISTERED Pipeline with an input built
from the event payload, via the async/detached ``start_pipeline_run`` (the
same call the ``run_pipeline_async`` tool verb makes) — fire-and-continue;
the pipeline runs in its own recoverable driver-session and its result
arrives later on the hook's own session inbox as a ``pipeline_result``
message.

Coverage plan
-------------
Tier 1 (contract): ``pipeline_launch`` schema shape + the loader's mutual-
  exclusion / required-field validation.
Tier 2 (OS invariant, dispatcher-unit): ``_dispatch_one`` renders
  ``input_template`` against ``template_vars`` and calls the injected
  ``launch_pipeline`` seam with the rendered dict; a render failure or a
  missing ``launch_pipeline`` callable logs + skips (never raises out of
  ``dispatch()``).
Tier 2 (OS invariant, end-to-end): a REAL Session + REAL AgentRegistry +
  REAL PipelineRegistry + a REAL registered Pipeline — the hook actually
  LAUNCHES the pipeline (observable side effect + ``pipeline_result``
  delivered to the invoking session's inbox), with the input threaded from
  ``template_vars``; an UNregistered pipeline name logs + skips without
  crashing the dispatcher (a sibling hook on the same point still fires).

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. The dispatcher-unit
tests use a real recording async callable (the same idiom as
test_hook_dispatcher_1800_5b.py's ``_Recorder``); the end-to-end tests use a
real ``Session``/``AgentRegistry``/``PipelineRegistry``/``PipelineExecutor``,
mirroring test_pipeline_is2_driver_session.py's fixture pattern (the only
"fake" is a scripted LLM callable injected through the real
``RouterLoopDriver`` ``_loop_observer`` seam — the LLM is incidental to what's
under test).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, ToolStep
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookConfigError, HookDef, PipelineLaunchBlock, PushBlock
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import PresentationWiring, ReactivityConfig
from tests._support.agent_session import make_session

# ---------------------------------------------------------------------------
# Recording seam (mirrors test_hook_dispatcher_1800_5b.py's _Recorder)
# ---------------------------------------------------------------------------


class _Recorder:
    """A real recording async callable — the generic seam stand-in for
    ``put_inbox``/``stage_next_turn_context``/``run_shell`` (accepts any
    args/kwargs, mirroring test_hook_dispatcher_1800_5b.py's ``_Recorder``)."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self._raises = raises

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises


class _LaunchRecorder:
    """A real recording async callable standing in for ``launch_pipeline``
    (the strict (name, input_data) signature)."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, "dict | None"]] = []
        self._raises = raises

    async def __call__(self, name: str, input_data: "dict | None") -> None:
        self.calls.append((name, input_data))
        if self._raises is not None:
            raise self._raises


def _dispatcher(hooks: list[HookDef], **seams) -> "tuple[HookDispatcher, dict]":
    seams.setdefault("put_inbox", _Recorder())
    seams.setdefault("stage_next_turn_context", _Recorder())
    seams.setdefault("run_shell", _Recorder())
    seams.setdefault("launch_pipeline", _LaunchRecorder())
    disp = HookDispatcher(
        HookRegistry(hooks),
        put_inbox=seams["put_inbox"],
        stage_next_turn_context=seams["stage_next_turn_context"],
        run_shell=seams["run_shell"],
        launch_pipeline=seams["launch_pipeline"],
    )
    return disp, seams


# ===========================================================================
# Tier 1 — Contract: schema shape + loader validation
# ===========================================================================


def test_hookdef_pipeline_launch_shape() -> None:
    """Tier 1: ``HookDef`` with a ``PipelineLaunchBlock`` carries the expected
    fields; the other three action fields stay unset."""
    block = PipelineLaunchBlock(name="digest", input_template={"n": "{{ event.n }}"})
    hd = HookDef(on="turn_end", pipeline_launch=block)

    assert hd.pipeline_launch is block
    assert hd.pipeline_launch.name == "digest"
    assert hd.pipeline_launch.input_template == {"n": "{{ event.n }}"}
    assert hd.template_push is None
    assert hd.shell_exec is None
    assert hd.shell_push is None


def test_load_hooks_pipeline_launch_parses() -> None:
    """Tier 1: a ``hooks:`` entry with ``pipeline_launch`` parses through the
    REAL ``load_hooks`` seam into a ``HookDef`` carrying the block, both with
    and without ``input_template``."""
    from reyn.hooks.loader import load_hooks

    raw = [
        {
            "on": "session_start",
            "pipeline_launch": {"name": "digest", "input_template": {"a": "{{ x }}"}},
        },
        {"on": "turn_end", "pipeline_launch": {"name": "no-input"}},
    ]
    registry = load_hooks(raw)
    (h1,) = registry.hooks_for("session_start")
    (h2,) = registry.hooks_for("turn_end")
    assert h1.pipeline_launch.name == "digest"
    assert h1.pipeline_launch.input_template == {"a": "{{ x }}"}
    assert h2.pipeline_launch.name == "no-input"
    assert h2.pipeline_launch.input_template is None


def test_load_hooks_pipeline_launch_missing_name_rejected() -> None:
    """Tier 1: a ``pipeline_launch`` block without ``name`` raises
    ``HookConfigError``."""
    from reyn.hooks.loader import load_hooks

    with pytest.raises(HookConfigError, match="pipeline_launch.name is required"):
        load_hooks([{"on": "turn_end", "pipeline_launch": {}}])


def test_load_hooks_pipeline_launch_mutually_exclusive_with_template_push() -> None:
    """Tier 1: ``pipeline_launch`` alongside ``template_push`` on the same
    entry raises ``HookConfigError`` (the 4-way mutual exclusion, #2608 H3)."""
    from reyn.hooks.loader import load_hooks

    with pytest.raises(HookConfigError, match="mutually exclusive"):
        load_hooks([
            {
                "on": "turn_end",
                "template_push": {"message": "hi"},
                "pipeline_launch": {"name": "p"},
            },
        ])


# ===========================================================================
# Tier 2 — dispatcher-unit: routing + rendering (recording seam)
# ===========================================================================


@pytest.mark.asyncio
async def test_pipeline_launch_routes_to_launch_pipeline_with_rendered_input():
    """Tier 2: a ``pipeline_launch`` hook renders ``input_template`` against
    ``template_vars`` and calls the injected ``launch_pipeline`` seam with the
    rendered dict — the input is ACTUALLY threaded from the event payload
    (not just passed through statically)."""
    hook = HookDef(
        on="mcp_resource_updated",
        pipeline_launch=PipelineLaunchBlock(
            name="digest", input_template={"uri": "{{ event.uri }}", "static": 1},
        ),
    )
    disp, seams = _dispatcher([hook])

    await disp.dispatch("mcp_resource_updated", {"event": {"uri": "resource://x"}})

    assert seams["launch_pipeline"].calls == [
        ("digest", {"uri": "resource://x", "static": 1}),
    ]
    # No push/shell path touched — pipeline_launch is its own scheme branch.
    assert seams["put_inbox"].calls == []
    assert seams["stage_next_turn_context"].calls == []
    assert seams["run_shell"].calls == []


@pytest.mark.asyncio
async def test_pipeline_launch_with_no_input_template_launches_with_none():
    """Tier 2: ``input_template`` absent (None) → the pipeline launches with
    ``input=None`` — no accidental empty-dict substitution."""
    hook = HookDef(on="turn_end", pipeline_launch=PipelineLaunchBlock(name="no-input"))
    disp, seams = _dispatcher([hook])

    await disp.dispatch("turn_end", {})

    assert seams["launch_pipeline"].calls == [("no-input", None)]


@pytest.mark.asyncio
async def test_pipeline_launch_render_failure_skips_without_crashing():
    """Tier 2: an ``input_template`` that fails to render (bad Jinja2 syntax)
    logs + skips the launch — ``dispatch()`` never raises, and
    ``launch_pipeline`` is never called."""
    hook = HookDef(
        on="turn_end",
        pipeline_launch=PipelineLaunchBlock(name="p", input_template="{{ not valid jinja !!"),
    )
    disp, seams = _dispatcher([hook])

    await disp.dispatch("turn_end", {})  # must not raise

    assert seams["launch_pipeline"].calls == []


@pytest.mark.asyncio
async def test_pipeline_launch_missing_callable_skips_without_crashing():
    """Tier 2: no ``launch_pipeline`` callable was injected (the default None)
    — a ``pipeline_launch`` hook logs + is skipped, siblings still proceed."""
    hooks = [
        HookDef(on="turn_end", pipeline_launch=PipelineLaunchBlock(name="p")),
        HookDef(on="turn_end", shell_exec="echo sibling"),
    ]
    disp = HookDispatcher(
        HookRegistry(hooks),
        put_inbox=_Recorder(),
        stage_next_turn_context=_Recorder(),
        run_shell=(shell_recorder := _Recorder()),
        # launch_pipeline intentionally omitted -> defaults to None
    )

    await disp.dispatch("turn_end", {})  # must not raise

    (args, _kwargs), = shell_recorder.calls
    assert args[0] == "echo sibling"


@pytest.mark.asyncio
async def test_pipeline_launch_raising_callable_isolated_siblings_proceed():
    """Tier 2: a ``launch_pipeline`` callable that raises (e.g. simulating an
    unregistered pipeline propagating past the session-level catch) is caught
    by the per-hook isolation — the sibling hook still runs."""
    raising = _LaunchRecorder(raises=RuntimeError("boom"))
    hooks = [
        HookDef(on="turn_end", pipeline_launch=PipelineLaunchBlock(name="missing")),
        HookDef(on="turn_end", shell_exec="echo sibling"),
    ]
    disp, seams = _dispatcher(hooks, launch_pipeline=raising)

    await disp.dispatch("turn_end", {})  # must not raise

    assert raising.calls == [("missing", None)]
    (args, _kwargs), = seams["run_shell"].calls
    assert args[0] == "echo sibling"


# ===========================================================================
# Tier 2 — end-to-end: real Session + real AgentRegistry + real Pipeline
# ===========================================================================


class _ScriptedAgentReply:
    """Always answers with one fixed plain-text turn — the LLM is incidental
    to what's under test (same rationale as test_pipeline_is2_driver_session.py)."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls += 1
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _install_side_effect_tool(monkeypatch) -> None:
    """Register a REAL side-effecting tool (appends a line to a file) through
    the production tool registry — mirrors test_pipeline_is2_driver_session.py's
    ``_install_side_effect_tool``."""
    import reyn.tools as tools_pkg
    from reyn.tools.types import ToolDefinition, ToolGates

    async def _handler(args, ctx):
        p = Path(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(str(args["line"]) + "\n")
        return {"line": str(args["line"])}

    tool = ToolDefinition(
        name="h3_append",
        description="H3 test: append a line to a file (real side effect).",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow", phase="allow"),
        handler=_handler,
        category="io",
        purity="side_effect",
    )
    base_build = tools_pkg.get_default_registry

    def _with_tool():
        reg = base_build()
        reg.register(tool)
        return reg

    monkeypatch.setattr(tools_pkg, "get_default_registry", _with_tool)


def _one_step_pipeline(out_file: Path) -> Pipeline:
    from reyn.core.pipeline.executor import ExprRef

    return Pipeline(
        steps=[
            ToolStep(
                name="h3_append",
                args={"path": str(out_file), "line": ExprRef("ctx.number")},
                output="t",
            ),
        ],
        description="H3 test pipeline",
    )


def _agent_registry_with_hooks(
    tmp_path: Path, state_log: "StateLog", hooks_config: list,
    pipeline_registry: "PipelineRegistry", scripted: "_ScriptedAgentReply | None",
) -> AgentRegistry:
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = make_session(
            presentation_wiring=PresentationWiring(presentation_consumer=presentation_consumer, intervention_bridge=intervention_bridge),
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            reactivity=ReactivityConfig(hooks_config=hooks_config), pipeline_registry=pipeline_registry,
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


async def _wait_for(pred, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_e2e_hook_launches_registered_pipeline_with_threaded_input(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: THE core H3 proof. A REAL Session with a ``pipeline_launch``
    hook configured launches a REAL registered Pipeline via the production DI
    wiring (``Session._launch_pipeline_from_hook`` -> ``start_pipeline_run``):
    the launched pipeline's real tool side effect proves the input was
    THREADED from ``template_vars`` (not a static default), and the terminal
    ``pipeline_result`` is delivered back to the invoking session (a scripted-
    LLM turn consumes it)."""
    _install_side_effect_tool(monkeypatch)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"

    pipeline_registry = PipelineRegistry()
    pipeline_registry.register("digest", _one_step_pipeline(out_file))

    hooks_config = [
        {
            "on": "session_start",
            "pipeline_launch": {
                "name": "digest",
                "input_template": {"number": "{{ event.count }}"},
            },
        },
    ]
    scripted = _ScriptedAgentReply("acknowledged")
    reg = _agent_registry_with_hooks(
        tmp_path, state_log, hooks_config, pipeline_registry, scripted,
    )
    caller = reg.get_or_load("worker")

    await caller._hook_dispatcher.dispatch("session_start", {"event": {"count": 7}})

    assert await _wait_for(lambda: out_file.exists() and out_file.read_text(encoding="utf-8").strip())
    assert out_file.read_text(encoding="utf-8").splitlines() == ["7"]
    # The invoker actually consumed the pipeline_result (a scripted-LLM turn ran).
    assert await _wait_for(lambda: scripted.calls >= 1)


@pytest.mark.asyncio
async def test_e2e_unregistered_pipeline_name_logs_and_skips_dispatcher_survives(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a ``pipeline_launch`` naming an UNregistered pipeline logs a
    clear warning and is skipped — the dispatcher survives (no crash) and a
    SIBLING hook on the same point still fires (proven via the real Session
    inbox, the same public surface test_hook_dispatcher_1800_5b.py uses)."""
    _install_side_effect_tool(monkeypatch)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    pipeline_registry = PipelineRegistry()  # deliberately empty — "digest" unregistered

    hooks_config = [
        {"on": "session_start", "pipeline_launch": {"name": "does-not-exist"}},
        {
            "on": "session_start",
            "template_push": {"message": "sibling ran", "wake": True},
        },
    ]
    reg = _agent_registry_with_hooks(
        tmp_path, state_log, hooks_config, pipeline_registry, None,
    )
    caller = reg.get_or_load("worker")

    await caller._hook_dispatcher.dispatch("session_start", {})  # must not raise

    # The sibling template_push hook still landed in the (public) inbox —
    # proof the unregistered pipeline_launch did not crash the dispatcher.
    kind, payload = caller.inbox.get_nowait()
    assert kind == "hook"
    assert payload["text"] == "sibling ran"
