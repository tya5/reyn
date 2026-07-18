"""Tier 2: OS invariant — #3093 a spawned pipeline driver-session must resolve a
``call``/``match`` sibling against the LAUNCHING caller's CURRENT ``PipelineRegistry``,
not the frozen per-frontend ``SessionFactoryConfig`` snapshot every spawn otherwise
inherits.

Root cause (confirmed by direct reproduction, not the plugin-install registration
path the originating dogfood witness suspected — that mechanism (multi-document
pipeline parsing + ``{key}.{name}`` namespacing, ``reyn/data/pipelines/registry.py``)
was independently verified correct): ``SessionFactoryConfig.pipeline_registry`` is
built ONCE per frontend at startup and threaded to every spawned ``Session``
(``factory_config.py``'s own docstring: "Threaded to every Session (incl. spawns,
which reuse this bundle)"). The hot-reload seam (``Session._reapply_pipelines``)
only mutates the LAUNCHING session's own registry (dual-write onto ``Session`` +
its ``RouterHostAdapter``) — it never touches the shared, frozen
``SessionFactoryConfig`` bundle. So a pipeline installed (or updated) mid-
conversation resolves fine when the CALLER looks its name up
(``pipeline_registry.get(name)`` — the caller's own registry is live), but the
PIPELINE DRIVER SESSION spawned to actually RUN it
(``_spawn_pipeline_driver_session`` in ``session_api.py``) starts with whatever
registry ITS OWN ``Session.__init__`` was given — the stale pre-install snapshot
in production (a real chat session), or (in this test) simply the DEFAULT empty
``PipelineRegistry`` every bare test-factory session gets when no registry is
threaded through it.

The main/top-level pipeline itself always "resolves" regardless (its whole
``Pipeline`` dataclass is serialized BY VALUE into ``invocation.json`` — no registry
lookup needed to find itself), but a ``call``/``match`` step's SIBLING target IS
resolved BY NAME against the driver-session's OWN registry at run time
(``PipelineExecutorDriver._pipeline_registry()`` -> ``RouterHostAdapter
.get_pipeline_registry()``) — exactly the symptom a plugin-installed multi-document
pipeline file hits when its main pipeline calls a same-file sibling (issue #3093):
the main pipeline's own steps run fine, but the ``call``/``match`` step targeting the
sibling fails "is not registered".

Fix: ``_spawn_pipeline_driver_session`` (``session_api.py``) accepts an optional
``pipeline_registry`` override; ``run_pipeline_attached``/``start_pipeline_run``
forward the LAUNCHING caller's own CURRENT ``pipeline_registry`` into it — threaded
from ``pipeline_verbs.py``'s ``_handle_run_pipeline`` (sync, derives it from the
caller session ``run_pipeline_attached`` already resolves internally),
``_handle_run_pipeline_async`` and ``_handle_run_pipeline_inline_async`` (both
already had ``rs.pipeline_registry`` in local scope), and
``Session._launch_pipeline_from_hook``. ``Session.set_pipeline_registry`` (the same
dual-write ``_reapply_pipelines`` already used) applies the override onto the
freshly-spawned driver-session right after spawn.

Real ``AgentRegistry``/``Session``/``StateLog``/``PipelineExecutor`` throughout (no
mocks) — mirrors ``tests/test_pipeline_is2_driver_session.py``'s harness, driving
the REAL production tool-verb entry points (``_handle_run_pipeline`` /
``_handle_run_pipeline_async``) rather than the lower-level ``session_api``
functions directly, so the exact code path a real chat session takes is exercised.

Coverage:
  1. Async detached (``_handle_run_pipeline_async`` — the ``run_pipeline_async``
     tool's real handler): a caller whose OWN registry carries both the main
     pipeline and a sibling it ``call``s launches successfully and the sibling
     actually runs.
  2. Sync attached (``_handle_run_pipeline`` — the ``run_pipeline`` tool's real
     handler): same scenario, attached path.
  3. Strip-falsify (CLAUDE.md testing policy): calling the internal spawn helper
     directly WITHOUT the ``pipeline_registry`` override (the exact pre-#3093 call
     shape) reproduces the dogfood-witnessed "target pipeline ... is not
     registered" failure verbatim — proving the two passing tests above are not
     vacuously green.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import CallStep, Pipeline, ToolStep
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.core.pipeline.work_order import pipeline_run_dir, read_result
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import _spawn_pipeline_driver_session
from reyn.tools.pipeline_verbs import _handle_run_pipeline, _handle_run_pipeline_async
from reyn.tools.types import RouterCallerState, ToolContext


def _agent_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real AgentRegistry + real Session factory (mirrors
    ``test_pipeline_is2_driver_session.py``'s ``_agent_registry``). Every spawned
    session — including a driver-session — gets the DEFAULT (empty) PipelineRegistry
    ``Session.__init__`` builds when none is threaded in: this IS the production
    "frozen per-frontend snapshot" a real chat deployment reuses across spawns,
    reproduced here without needing a real ``SessionFactoryConfig``."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
        )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


def _install_side_effect_tool(monkeypatch, out_file: Path) -> None:
    """Real side-effecting tool: appends a line per call — proof the sibling
    pipeline's OWN step actually ran (not just that ``call`` resolved)."""
    import reyn.tools as tools_pkg
    from reyn.tools.types import ToolDefinition, ToolGates

    async def _handler(args, ctx):
        p = Path(out_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(str(args.get("tag", "x")) + "\n")
        return {"tag": str(args.get("tag", "x"))}

    tool = ToolDefinition(
        name="p3093_step",
        description="#3093 test: append a line per call (real side effect).",
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


def _main_and_sibling_registry() -> "tuple[Pipeline, PipelineRegistry]":
    """A namespaced main+sibling pair mirroring #3093's real shape (#2722
    ``{key}.{name}`` namespacing already resolved, as the loader would have): the
    main pipeline ``call``s ``ns.sibling`` by its GLOBAL dotted name; the sibling
    runs one real tool step. Both registered under a fresh ``PipelineRegistry`` —
    the CALLER's own, live, post-hot-reload registry in the real flow."""
    sibling = Pipeline(steps=[
        ToolStep(name="p3093_step", args={"tag": "sibling-ran"}, output="o0"),
    ])
    main = Pipeline(steps=[
        CallStep(pipeline="ns.sibling", output="result"),
    ])
    registry = PipelineRegistry()
    registry.register("ns.main", main)
    registry.register("ns.sibling", sibling)
    return main, registry


async def _wait_for(pred, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_async_detached_run_resolves_sibling_via_caller_registry(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the REAL ``run_pipeline_async`` tool handler
    (``_handle_run_pipeline_async``) — a driver-session spawned to run ``ns.main``
    resolves its ``call`` to ``ns.sibling`` because the LAUNCHING caller's live
    ``ctx.router_state.pipeline_registry`` is forwarded to the spawn. RED before
    #3093's fix: the driver would inherit its own default EMPTY registry and fail
    "target pipeline 'ns.sibling' is not registered"."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_side_effect_tool(monkeypatch, out_file)
    reg = _agent_registry(tmp_path, state_log)

    main, caller_registry = _main_and_sibling_registry()
    caller = reg.get_or_load("worker")
    # Same dual-write the hot-reload seam (Session._reapply_pipelines) uses — sets
    # BOTH caller._pipeline_registry and caller._router_host._pipeline_registry, so
    # ctx.router_state.pipeline_registry (built from the SAME live registry, below)
    # and caller_session.pipeline_registry (what run_pipeline_attached re-derives
    # internally for the sync path) are the SAME object, matching production
    # (RouterCallerState.pipeline_registry is itself derived from
    # host.get_pipeline_registry() at each turn).
    caller.set_pipeline_registry(caller_registry)
    ctx = ToolContext(
        events=caller._router_host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            pipeline_registry=caller.pipeline_registry,
            agent_registry=reg,
            host=caller._router_host,
        ),
        state_log=state_log,
    )

    result = await _handle_run_pipeline_async({"name": "ns.main", "input": None}, ctx)
    assert result["status"] == "started", result
    run_id = result["data"]["run_id"]
    run_dir = pipeline_run_dir(tmp_path / ".reyn", run_id)

    assert await _wait_for(lambda: read_result(run_dir) is not None)
    terminal = read_result(run_dir)
    assert terminal["status"] == "ok", terminal
    # The sibling's OWN step really ran (not just that `call` resolved a stub).
    assert out_file.read_text(encoding="utf-8").splitlines() == ["sibling-ran"]


@pytest.mark.asyncio
async def test_sync_attached_run_resolves_sibling_via_caller_registry(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the REAL ``run_pipeline`` tool handler (``_handle_run_pipeline``,
    sync attached) — same scenario. RED before #3093's fix for the same reason."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_side_effect_tool(monkeypatch, out_file)
    reg = _agent_registry(tmp_path, state_log)

    main, caller_registry = _main_and_sibling_registry()
    caller = reg.get_or_load("worker")
    # Same dual-write the hot-reload seam (Session._reapply_pipelines) uses — sets
    # BOTH caller._pipeline_registry and caller._router_host._pipeline_registry, so
    # ctx.router_state.pipeline_registry (built from the SAME live registry, below)
    # and caller_session.pipeline_registry (what run_pipeline_attached re-derives
    # internally for the sync path) are the SAME object, matching production
    # (RouterCallerState.pipeline_registry is itself derived from
    # host.get_pipeline_registry() at each turn).
    caller.set_pipeline_registry(caller_registry)
    ctx = ToolContext(
        events=caller._router_host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            pipeline_registry=caller.pipeline_registry,
            agent_registry=reg,
            host=caller._router_host,
        ),
        state_log=state_log,
    )

    result = await _handle_run_pipeline({"name": "ns.main", "input": None}, ctx)
    assert result["status"] == "ok", result
    assert out_file.read_text(encoding="utf-8").splitlines() == ["sibling-ran"]


@pytest.mark.asyncio
async def test_strip_falsify_no_registry_override_reproduces_3093(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: strip-falsify (CLAUDE.md testing policy) — calling the internal spawn
    helper WITHOUT the ``pipeline_registry`` override (the exact pre-#3093 call
    shape) reproduces the dogfood-witnessed failure verbatim — proving the two
    passing tests above are not vacuously green. The driver-session's OWN
    (default-empty) registry cannot resolve ``ns.sibling``, even though the SAME
    name is perfectly resolvable on the caller's own registry."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_side_effect_tool(monkeypatch, out_file)
    reg = _agent_registry(tmp_path, state_log)

    main, caller_registry = _main_and_sibling_registry()
    caller = reg.get_or_load("worker")
    caller.set_pipeline_registry(caller_registry)
    # Sanity: the caller itself resolves the sibling fine (the bug is spawn-local,
    # not a broken registry contents/namespacing).
    assert caller.pipeline_registry.get("ns.sibling") is not None

    session, rid, sid = await _spawn_pipeline_driver_session(
        reg,
        pipeline=main,
        pipeline_name="ns.main",
        input=None,
        reply_to_agent="worker",
        reply_to_sid="main",
        state_log=state_log,
        notify_reply=False,
        # pipeline_registry intentionally OMITTED — the pre-#3093 call shape.
    )
    from reyn.runtime.message_bus import MessageBus
    from reyn.runtime.transport import SystemRef

    bus = MessageBus()
    await bus.request(
        session, kind="user", payload={"text": "", "chain_id": "chain"},
        reply_to=SystemRef(), timeout=15.0,
    )

    run_dir = pipeline_run_dir(tmp_path / ".reyn", rid)
    result = read_result(run_dir)
    assert result is not None
    assert result["status"] == "failed", result
    assert "ns.sibling" in result["error"] and "not registered" in result["error"], result
    # The sibling's step never ran — the failure is the lookup, not a broader break.
    assert not out_file.exists()
