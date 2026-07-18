"""Tier 2: OS invariant — #3093 a spawned pipeline driver-session must resolve a
``call``/``match`` sibling against the CURRENT on-disk pipeline config, not the
frozen per-frontend ``SessionFactoryConfig`` snapshot every spawn otherwise
inherits.

Root cause (confirmed by direct reproduction, not the plugin-install registration
path the originating dogfood witness suspected — that mechanism (multi-document
pipeline parsing + ``{key}.{name}`` namespacing, ``reyn/data/pipelines/registry.py``)
was independently verified correct): ``SessionFactoryConfig.pipeline_registry`` is
built ONCE per frontend at startup and threaded to every spawned ``Session``
(``factory_config.py``'s own docstring: "Threaded to every Session (incl. spawns,
which reuse this bundle)"). So a pipeline installed (or updated) mid-conversation
resolves fine when the CALLER looks its name up (its own registry is live via the
hot-reload seam), but the PIPELINE DRIVER SESSION spawned to actually RUN it
(``_spawn_pipeline_driver_session`` in ``session_api.py``) started with whatever
registry ITS OWN ``Session.__init__`` was given — the stale pre-install snapshot.

The main/top-level pipeline itself always "resolves" regardless (its whole
``Pipeline`` dataclass is serialized BY VALUE into ``invocation.json`` — no registry
lookup needed to find itself), but a ``call``/``match`` step's SIBLING target IS
resolved BY NAME against the driver-session's OWN registry at run time
(``PipelineExecutorDriver._pipeline_registry()`` -> ``RouterHostAdapter
.get_pipeline_registry()``) — exactly the symptom a plugin-installed multi-document
pipeline file hits when its main pipeline calls a same-file sibling (issue #3093):
the main pipeline's own steps run fine, but the ``call``/``match`` step targeting the
sibling fails "is not registered".

#3094 point-fixed this by threading the LAUNCHING caller's live in-memory
``PipelineRegistry`` into the spawn (``_spawn_pipeline_driver_session``'s
``pipeline_registry`` override + ``Session.set_pipeline_registry``). #3097
(this revision) FOLDS THAT OUT: the config-projection refresh family-gate
(``AgentRegistry.spawn_session_recorded`` -> ``Session.refresh_config_projections()``)
now fires ``Session._reapply_pipelines`` uniformly on EVERY programmatic spawn
(the same funnel ``_spawn_pipeline_driver_session`` uses), rebuilding the
driver-session's OWN registry from the CURRENT ON-DISK config cascade — no
explicit caller hand-off needed. This is equivalent to (and supersedes) the
#3094 point-fix given the confirmed topology (#3061): an install always writes
to disk BEFORE the spawn that consumes it (no code path installs and consumes
in the same run), so a driver-session's own fresh disk-rebuild at ITS spawn
finds a just-installed sibling without needing the caller's in-memory copy.

Real ``AgentRegistry``/``Session``/``StateLog``/``PipelineExecutor`` throughout (no
mocks) — mirrors ``tests/test_pipeline_is2_driver_session.py``'s harness, driving
the REAL production tool-verb entry points (``_handle_run_pipeline`` /
``_handle_run_pipeline_async``) rather than the lower-level ``session_api``
functions directly, so the exact code path a real chat session takes is exercised.
Pipelines are installed the PRODUCTION way — an on-disk DSL file + a
``.reyn/config/pipelines.yaml`` entry (mirrors ``tests/test_2581_pipeline_hotreload.py``'s
``_write_pipeline``/``_write_dynamic_entries`` helpers) — so the family-gate's
disk-cascade rebuild has something real to find.

Coverage:
  1. Async detached (``_handle_run_pipeline_async`` — the ``run_pipeline_async``
     tool's real handler): a main pipeline ``call``ing a same-file sibling,
     installed to disk before the spawn, launches successfully and the sibling
     actually runs.
  2. Sync attached (``_handle_run_pipeline`` — the ``run_pipeline`` tool's real
     handler): same scenario, attached path.
  3. Strip-falsify (CLAUDE.md testing policy): spawning the driver-session via
     the LOWER-LEVEL ``spawn_session`` (the crash-recovery re-wake call shape,
     which never fires the family gate) instead of ``spawn_session_recorded``
     reproduces the exact "target pipeline ... is not registered" failure —
     proving (a) the two passing tests above are not vacuously green, and (b)
     the family gate (not something else) is what makes them pass.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.work_order import pipeline_run_dir, read_result
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.tools.pipeline_verbs import _handle_run_pipeline, _handle_run_pipeline_async
from reyn.tools.types import RouterCallerState, ToolContext


def _agent_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real AgentRegistry + real Session factory (mirrors
    ``test_pipeline_is2_driver_session.py``'s ``_agent_registry``). Every spawned
    session builds its ``PipelineRegistry`` from whatever ``pipelines.entries``
    the config cascade declared AT FACTORY-CONSTRUCTION time (built once, closed
    over below) — this IS the production "frozen per-frontend snapshot" a real
    chat deployment reuses across spawns."""
    from reyn.config.loader import load_config
    from reyn.data.pipelines.registry import build_pipeline_registry

    if not (tmp_path / "reyn.yaml").exists():
        (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    holder: dict = {}
    frozen_registry = build_pipeline_registry(load_config(tmp_path).pipelines, tmp_path)

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
            pipeline_registry=frozen_registry,
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


_MAIN_AND_SIBLING_DSL = """
pipeline: main
steps:
  - call: {pipeline: sibling, output: result}
---
pipeline: sibling
steps:
  - tool: {name: p3093_step, args: {tag: sibling-ran}, output: o0}
"""


def _install_pipeline_to_disk(tmp_path: Path, *, key: str = "ns") -> None:
    """Production-shaped install: an on-disk DSL file (multi-document, main +
    same-file sibling) declared via ``.reyn/config/pipelines.yaml``'s
    ``pipelines.entries`` — mirrors a real ``plugin_install``/``pipeline_install``
    write, NOT an in-memory-only registry (the family gate's ``_reapply_pipelines``
    rebuilds from THIS disk cascade, so a test must actually write it)."""
    d = tmp_path / "pipelines"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ns.yaml").write_text(_MAIN_AND_SIBLING_DSL, encoding="utf-8")
    cfg_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.dump({"pipelines": {"entries": {key: {"path": "pipelines/ns.yaml"}}}}),
        encoding="utf-8",
    )


async def _wait_for(pred, timeout: float = 15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_async_detached_run_resolves_sibling_via_family_gate(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the REAL ``run_pipeline_async`` tool handler
    (``_handle_run_pipeline_async``) — a driver-session spawned to run
    ``ns.main`` resolves its ``call`` to ``ns.sibling`` because
    ``spawn_session_recorded`` fires ``refresh_config_projections()`` on the
    freshly-spawned driver-session, which rebuilds its pipeline registry from
    the on-disk cascade BEFORE the run's first nudge. RED before #3097 (and
    before #3094): the driver would inherit the caller's FROZEN factory-time
    registry and fail "target pipeline 'ns.sibling' is not registered"."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_side_effect_tool(monkeypatch, out_file)
    reg = _agent_registry(tmp_path, state_log)

    # Install AFTER the registry/session factory captured its frozen snapshot
    # (mirrors production: install happens mid-conversation, after boot).
    _install_pipeline_to_disk(tmp_path)

    caller = reg.get_or_load("worker")

    # The caller must itself hot-reload before it can even resolve ns.main by
    # name to launch it (mirrors dispatch_install_reload firing on the caller
    # right after the real install op writes the config). MUST run BEFORE the
    # ctx below captures pipeline_registry — _reapply_pipelines SWAPS the
    # reference (never mutates in place), so a ctx built first would still
    # hold the stale pre-swap object.
    changed = await caller._reapply_pipelines({})
    assert changed is True
    assert caller.pipeline_registry.get("ns.main") is not None

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
async def test_sync_attached_run_resolves_sibling_via_family_gate(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the REAL ``run_pipeline`` tool handler (``_handle_run_pipeline``,
    sync attached) — same scenario. RED before #3097 for the same reason."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_side_effect_tool(monkeypatch, out_file)
    reg = _agent_registry(tmp_path, state_log)
    _install_pipeline_to_disk(tmp_path)

    caller = reg.get_or_load("worker")
    await caller._reapply_pipelines({})
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
async def test_strip_falsify_spawn_session_bypasses_family_gate_reproduces_3093(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: strip-falsify (CLAUDE.md testing policy) — spawning the driver-
    session via the LOWER-LEVEL ``AgentRegistry.spawn_session`` (the exact call
    shape crash-recovery re-wake uses, which never fires
    ``refresh_config_projections()``) instead of the production
    ``_spawn_pipeline_driver_session``/``spawn_session_recorded`` path
    reproduces the dogfood-witnessed "target pipeline ... is not registered"
    failure verbatim — proving the two passing tests above are not vacuously
    green, AND that the family gate (fired only by ``spawn_session_recorded``)
    is the actual mechanism making them pass, not some other incidental
    freshness."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    out_file = tmp_path / "out.txt"
    _install_side_effect_tool(monkeypatch, out_file)
    reg = _agent_registry(tmp_path, state_log)
    _install_pipeline_to_disk(tmp_path)

    caller = reg.get_or_load("worker")
    await caller._reapply_pipelines({})
    main = caller.pipeline_registry.get("ns.main")
    assert main is not None  # sanity: the caller itself resolves it fine

    # The pre-#3097 (and pre-#3094) call shape: spawn WITHOUT going through
    # spawn_session_recorded (no family-gate refresh) — the crash-recovery
    # re-wake shape.
    from reyn.runtime.spawn_routing import ReviewedNA
    routing = ReviewedNA("runtime/registry.py::restore_all")
    sid = reg.spawn_session(
        "worker",
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    from reyn.core.pipeline.serde import pipeline_to_dict
    from reyn.core.pipeline.work_order import (
        PipelineWorkOrder,
        write_invocation,
    )
    from reyn.core.pipeline.work_order import (
        pipeline_run_dir as _run_dir_fn,
    )
    from reyn.runtime.services.pipeline_executor_driver import PipelineExecutorDriver

    rid = "run-3093-strip-falsify"
    work_order = PipelineWorkOrder(
        run_id=rid, pipeline_name="ns.main", pipeline=pipeline_to_dict(main),
        input=None, reply_to_agent="worker", reply_to_sid="main",
        driver_agent="worker", driver_sid=sid,
    )
    write_invocation(_run_dir_fn(tmp_path / ".reyn", rid), work_order)
    session = reg.get_session("worker", sid)
    assert session is not None
    session.set_loop_driver(PipelineExecutorDriver(
        work_order, registry=reg, state_log=state_log, notify_reply=False,
    ))

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
