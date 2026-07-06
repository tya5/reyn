"""Tier 2: #2581 — pipeline hot-reload via the HotReloader seam.

Mirrors ``test_2548_skill_hotreload_toggle_pr_b.py``'s skills reload coverage,
for the ``pipelines/`` disk-loader (#2575's ``build_pipeline_registry``)
instead of the skills registry. Three invariants:

1. **Hot-reload e2e**: adding/changing a ``pipelines/*.yaml`` file and applying
   the ``pipelines`` seam (``Session._reapply_pipelines``) swaps BOTH
   ``session.pipeline_registry`` and ``session.router_host.get_pipeline_registry()``
   — the dual-write the ``RouterHostAdapter`` needs, since it holds its own
   ``_pipeline_registry`` captured at construction and never re-reads Session
   (exactly like ``_available_skills`` / ``_reapply_skills``).
2. **Malformed-on-reload**: a broken ``pipelines/*.yaml`` file at reload time
   makes the seam return False and leaves the OLD registry (on both holders)
   fully intact — last-good, atomic-by-construction (the new registry is only
   ever assigned after ``build_pipeline_registry`` succeeds), and the failure
   is observable via ``HotReloader.apply_pending()``'s ``failed`` list.
3. **Running-run immunity (R7)**: a pipeline run driven by
   ``PipelineExecutorDriver`` resolves ITS OWN definition from the snapshotted
   work-order (``pipeline_from_dict(wo.pipeline)``), never the live session
   registry — so a reload (even one that replaces the same-named pipeline
   registered under the live registry) cannot change an in-flight run's own
   steps/schema.

Policy compliance (docs/deep-dives/contributing/testing.md): real instances
only — no unittest.mock. Real Session / RouterHostAdapter / HotReloader /
PipelineExecutorDriver throughout.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, TransformStep
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.core.pipeline.serde import pipeline_to_dict
from reyn.core.pipeline.work_order import PipelineWorkOrder
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.pipeline_executor_driver import PipelineExecutorDriver
from reyn.runtime.session import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, agent_name: str = "test-agent") -> Session:
    """Minimal real Session in *tmp_path* with a live pipeline registry built
    from whatever ``pipelines.entries`` the config cascade currently declares
    (``reyn.yaml`` and/or the dynamic ``.reyn/config/pipelines.yaml`` — see
    ``_write_dynamic_entries``) — mirrors ``SessionFactoryConfig.from_config``'s
    production build-once path. A bare ``model: standard`` reyn.yaml (no
    ``pipelines:`` block) is also valid and yields an empty registry."""
    if not (tmp_path / "reyn.yaml").exists():
        (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    from reyn.config.loader import load_config
    from reyn.data.pipelines.registry import build_pipeline_registry
    cfg = load_config(tmp_path)
    registry = build_pipeline_registry(cfg.pipelines, tmp_path)
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        pipeline_registry=registry,
    )


def _write_pipeline(tmp_path: Path, filename: str, dsl_text: str) -> None:
    d = tmp_path / "pipelines"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(dsl_text, encoding="utf-8")


def _write_dynamic_entries(tmp_path: Path, *names_and_paths: "tuple[str, str]") -> None:
    """Write ``.reyn/config/pipelines.yaml`` declaring ``pipelines.entries`` —
    the IN-set dynamic file (mirrors ``.reyn/config/skills.yaml``), the
    runtime-mutable layer an install tool / operator edits between reloads
    (as opposed to the restart-only ``reyn.yaml``)."""
    import yaml
    path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump({"pipelines": {"entries": {n: {"path": p} for n, p in names_and_paths}}}),
        encoding="utf-8",
    )


_HELLO_V1 = """
pipeline: hello
description: v1 greeting
steps:
  - transform: {value: "'v1-' + ctx.name", output: greeting}
"""

_HELLO_V2 = """
pipeline: hello
description: v2 greeting
steps:
  - transform: {value: "'v2-' + ctx.name", output: greeting}
"""


def _names(session: Session) -> "set[str]":
    return set(session.pipeline_registry.names())


# ---------------------------------------------------------------------------
# 1. Hot-reload e2e: dual-write swap (Session + router_host adapter)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hotreload_adds_pipeline_to_live_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the pipelines seam picks up a NEW pipeline file and swaps the
    LIVE registry on both ``session.pipeline_registry`` and
    ``session.router_host.get_pipeline_registry()`` — not a dead local copy."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    assert "hello" not in _names(session)

    _write_pipeline(tmp_path, "hello.yaml", _HELLO_V1)
    _write_dynamic_entries(tmp_path, ("hello", "pipelines/hello.yaml"))

    changed = await session._reapply_pipelines({})

    assert changed is True
    assert "hello" in _names(session)
    assert "hello" in set(session.router_host.get_pipeline_registry().names()), (
        "the RouterHostAdapter's own captured registry must reflect the swap "
        "(the dual-write the adapter needs since it never re-reads Session)"
    )


@pytest.mark.asyncio
async def test_hotreload_changes_pipeline_description_on_live_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: replacing a pipeline file's content (same declared name, new
    description) is visible on the live registry after the seam applies —
    proves the swap is a fresh object, not a stale cached one."""
    monkeypatch.chdir(tmp_path)
    _write_pipeline(tmp_path, "hello.yaml", _HELLO_V1)
    _write_dynamic_entries(tmp_path, ("hello", "pipelines/hello.yaml"))
    session = _make_session(tmp_path)
    assert session.pipeline_registry.get("hello").steps[0].value.startswith("'v1-'")

    _write_pipeline(tmp_path, "hello.yaml", _HELLO_V2)
    changed = await session._reapply_pipelines({})

    assert changed is True
    assert session.pipeline_registry.get("hello").steps[0].value.startswith("'v2-'")
    assert (
        session.router_host.get_pipeline_registry().get("hello").steps[0].value.startswith("'v2-'")
    ), "router_host's own registry copy must also see the changed definition"


@pytest.mark.asyncio
async def test_hotreload_seam_registered(tmp_path: Path) -> None:
    """Tier 2: the Session registers the pipelines seam on the HotReloader."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    seam_names = [name for (name, _fn) in session._hot_reloader._seams]
    assert "pipelines" in seam_names


@pytest.mark.asyncio
async def test_hotreload_via_apply_pending_dual_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2c: the seam applies through the SAME path a real ``/reload`` uses
    — ``HotReloader.request_reload`` + ``apply_pending`` — and the dual-write
    swap is visible after that, not just via a direct seam call."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    _write_pipeline(tmp_path, "hello.yaml", _HELLO_V1)
    _write_dynamic_entries(tmp_path, ("hello", "pipelines/hello.yaml"))

    session._hot_reloader.request_reload(source="operator")
    summary = await session._hot_reloader.apply_pending()

    assert summary is not None
    assert "pipelines" in summary["applied"]
    assert "hello" in _names(session)
    assert "hello" in set(session.router_host.get_pipeline_registry().names())


# ---------------------------------------------------------------------------
# 2. Malformed-on-reload: last-good, atomic-by-construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hotreload_malformed_pipeline_keeps_old_registry_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a broken ``pipelines/*.yaml`` at reload time makes the seam
    return False and leaves the OLD registry (both holders) intact — the
    broken file does NOT half-apply or clear the live registry."""
    monkeypatch.chdir(tmp_path)
    _write_pipeline(tmp_path, "hello.yaml", _HELLO_V1)
    _write_dynamic_entries(tmp_path, ("hello", "pipelines/hello.yaml"))
    session = _make_session(tmp_path)
    assert "hello" in _names(session)
    old_registry = session.pipeline_registry

    # Break the file: malformed steps.
    _write_pipeline(tmp_path, "hello.yaml", "pipeline: hello\nsteps: not-a-list\n")

    changed = await session._reapply_pipelines({})

    assert changed is False
    assert session.pipeline_registry is old_registry, (
        "a failed rebuild must leave the Session's live registry object untouched"
    )
    assert session.router_host.get_pipeline_registry() is old_registry, (
        "a failed rebuild must leave the router_host's captured registry untouched"
    )
    assert "hello" in _names(session), "the last-good pipeline must still be registered"


@pytest.mark.asyncio
async def test_hotreload_malformed_pipeline_observable_via_warning_and_noop_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2c: a malformed pipeline file makes ``_reapply_pipelines`` catch +
    log the failure internally and return False (mirrors ``_reapply_skills``'s
    best-effort posture — the seam itself never raises), so it is absent from
    both ``applied`` and ``failed`` via ``apply_pending`` (a no-op reload for
    that component) — the failure is observable via the logged warning, giving
    the /reload operator visible feedback rather than a silent half-apply."""
    monkeypatch.chdir(tmp_path)
    _write_pipeline(tmp_path, "hello.yaml", _HELLO_V1)
    _write_dynamic_entries(tmp_path, ("hello", "pipelines/hello.yaml"))
    session = _make_session(tmp_path)

    _write_pipeline(tmp_path, "hello.yaml", "pipeline: hello\nsteps: not-a-list\n")
    session._hot_reloader.request_reload(source="operator")
    with caplog.at_level("WARNING", logger="reyn.runtime.session"):
        summary = await session._hot_reloader.apply_pending()

    assert summary is not None
    assert "pipelines" not in summary["applied"]
    assert "pipelines" not in summary["failed"]
    assert any(
        "_reapply_pipelines" in rec.message for rec in caplog.records
    ), "the malformed-file failure must be logged for operator visibility"
    # And the old registry is still intact after the no-op apply.
    assert "hello" in _names(session)


# ---------------------------------------------------------------------------
# 3. Running-run immunity (R7): the driver uses the snapshotted work-order
#    definition, never the live (possibly reloaded) registry, for its OWN steps.
# ---------------------------------------------------------------------------


def _worker_registry(tmp_path: Path, state_log: StateLog, pipeline_registry: PipelineRegistry) -> AgentRegistry:
    holder: dict = {}

    def _factory(profile) -> Session:
        return Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            pipeline_registry=pipeline_registry,
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    return reg


@pytest.mark.asyncio
async def test_running_pipeline_immune_to_registry_reload(tmp_path: Path) -> None:
    """Tier 2c: a pipeline run's OWN definition comes from the snapshotted
    work-order (``invocation.json`` equivalent — ``wo.pipeline``), NOT the live
    session registry. Even though the LIVE registry is reloaded to a DIFFERENT
    "hello" definition between work-order creation and the nudge, the run's
    own output reflects the ORIGINAL snapshot — confirming the immunity the
    #2581 design relies on (a reload can never change an in-flight run's own
    steps)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    # The LIVE registry has a "hello" pipeline that would produce
    # "from-live-registry" if (incorrectly) consulted for the run's own steps.
    live_registry = PipelineRegistry()
    live_registry.register(
        "hello",
        Pipeline(
            steps=[TransformStep(value="'from-live-registry'", output="greeting")],
            description="live", name="hello",
        ),
    )
    reg = _worker_registry(tmp_path, state_log, live_registry)
    caller = reg.get_or_load("worker")
    caller_host = caller._router_host  # noqa: SIM118 — seam-test assignment

    # The work-order's OWN snapshot is a DIFFERENT "hello" definition —
    # simulating a run launched before a later reload changed the on-disk def.
    snapshot_pipeline = Pipeline(
        steps=[TransformStep(value="'from-snapshot'", output="greeting")],
        description="snapshot", name="hello",
    )
    work_order = PipelineWorkOrder(
        run_id="run-2581-immunity", pipeline_name="hello",
        pipeline=pipeline_to_dict(snapshot_pipeline),
        input={}, reply_to_agent="worker", reply_to_sid="main",
        driver_agent="worker", driver_sid="drv1",
    )
    driver = PipelineExecutorDriver(work_order, registry=reg, state_log=state_log, notify_reply=False)
    driver.bind_session(caller, caller_host)

    # Simulate a reload landing on the LIVE registry BEFORE the nudge runs —
    # replace "hello" with yet another definition. The driver must still use
    # the work-order's own snapshot, not whatever the live registry now holds.
    reloaded_registry = PipelineRegistry()
    reloaded_registry.register(
        "hello",
        Pipeline(
            steps=[TransformStep(value="'from-reloaded-registry'", output="greeting")],
            description="reloaded", name="hello",
        ),
    )
    caller._pipeline_registry = reloaded_registry
    caller_host._pipeline_registry = reloaded_registry

    await driver.run_turn("nudge", chain_id="chain-2581")

    from reyn.core.pipeline.work_order import read_result
    result = read_result(driver._run_dir())
    assert result is not None
    assert result["status"] == "ok"
    assert result["named_stores"]["greeting"] == "from-snapshot", (
        "the run must use its OWN snapshotted definition, immune to a "
        "registry reload that happened before the nudge"
    )
