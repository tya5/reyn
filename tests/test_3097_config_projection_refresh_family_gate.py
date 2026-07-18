"""Tier 2: #3097 (#3061 follow-up) — the config-projection refresh family-gate.

#3061 closed the MCP-roster staleness gap for a programmatically-spawned session
(``AgentRegistry.spawn_session_recorded``, the funnel every agent-step ephemeral
worker / pipeline driver-session / ``session_spawn`` target shares) but explicitly
DEFERRED the other 9 ``_reapply_*`` hot-reload seams pending per-seam driven
verification. #3094 point-fixed ONE of those 9 (``_reapply_pipelines``) after it
surfaced live in the RAG turnkey flow — a curated-subset anti-pattern (#3096): the
family is enumerable at ``Session._register_hot_reload_seams()``, so this closes it
UNIFORMLY instead of point-fix×N.

``Session.refresh_config_projections()`` iterates EVERY registered hot-reload seam
(derived from ``HotReloader.seam_names()`` via ``Session.hot_reload_seam_names()``)
and fires each at the ephemeral/spawn action-boundary — EXCLUDING ``cron`` (the one
genuinely SIDE-EFFECTING seam: it mutates the global scheduler, which a short-lived
programmatic spawn must never reschedule on its own). ``AgentRegistry.
spawn_session_recorded`` calls it right after construction (replacing the #3061
MCP-only ``refresh_mcp_servers()`` call — MCP is now one family member among many,
covered by the SAME uniform iteration).

Structural contract this gate fixes in place:
  1. every ``_register_hot_reload_seams()`` member is covered UNIFORMLY, derived
     from the registry (never a hand-picked subset) — a future ``register_seam``
     addition is covered automatically, on registration, with no PR needed here;
  2. ``cron`` is the ONE documented exclusion (side-effecting, not a read-only
     projection);
  3. crash-recovery re-wake (``AgentRegistry.restore_all`` / ``_rewake_pipeline_runs``)
     NEVER fires this — those paths call the lower-level ``spawn_session`` directly,
     never ``spawn_session_recorded`` (the sole caller of ``refresh_config_projections``)
     — so a re-woken session's config projections reflect the RESTORED pre-crash
     snapshot, never silently overwritten with whatever the CURRENT on-disk config
     happens to be (snapshot fidelity);
  4. ``_reapply_visibility_override`` (security-core: visible ⊆ authorized) is
     included, and — being a restrict-only ∩ compose by construction — can only
     narrow the spawned session's envelope relative to its authorized base, never
     escalate beyond it.

No mocks: real ``AgentRegistry``/``Session``/``StateLog`` throughout, mirroring
``tests/test_3036_spawned_session_mcp_refresh.py``'s and
``tests/test_2581_pipeline_hotreload.py``'s harnesses.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import PresentationWiring
from reyn.runtime.spawn_routing import ReviewedNA


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """A registry whose ``session_factory`` — mirroring every real frontend's
    boot-time closure — captures every projection ONCE (empty/default), before any
    install writes fresher config. Only a spawn-time refresh can make a freshly
    spawned session see config written after the registry was built."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_wiring=PresentationWiring(presentation_consumer=presentation_consumer, intervention_bridge=intervention_bridge),
            mcp_servers={},
        )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("worker", role="").save(tmp_path / ".reyn" / "agents" / "worker")
    return reg


# ── 1. completeness gate: derived from the registry, vacuity-guarded ─────────


@pytest.mark.asyncio
async def test_vacuity_guard_registered_seams_nonempty(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: vacuity guard — a Session actually registers a non-empty set of
    hot-reload seams (a completeness gate over an accidentally-empty set would be
    trivially, uselessly green)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    assert len(session.hot_reload_seam_names()) > 0


@pytest.mark.asyncio
async def test_refresh_config_projections_covers_every_registered_seam_except_cron(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the completeness gate — ``refresh_config_projections()`` invokes
    EVERY seam ``hot_reload_seam_names()`` reports, except ``cron``, derived from
    the registry (not a hand-written subset). Strip-falsify (b): dropping a
    registered seam from ``_register_hot_reload_seams()`` would shrink
    ``hot_reload_seam_names()`` and this equality would still hold trivially — the
    REAL falsify is the reverse direction, proven below: a NEWLY registered seam
    (added here, mirroring a future ``register_seam`` call) is picked up
    automatically without touching ``refresh_config_projections`` — proving the
    coverage is structural (derives from the SAME registry), not a maintained
    marker list that a future addition could silently miss."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    registered = set(session.hot_reload_seam_names())
    assert registered, "vacuity guard"
    assert "cron" in registered

    result = await session.refresh_config_projections()
    assert set(result["invoked"]) == registered - {"cron"}
    assert "cron" not in result["invoked"]

    # A future family member, registered exactly like every other seam — proves
    # the family gate's coverage is DERIVED (auto-covers a new registration)
    # rather than a hand-picked subset that would need its own PR to extend.
    fired: "list[bool]" = []

    async def _future_seam(in_set: dict) -> bool:
        fired.append(True)
        return True

    session._hot_reloader.register_seam("future_member_3097", _future_seam)
    result2 = await session.refresh_config_projections()
    assert "future_member_3097" in result2["invoked"]
    assert fired == [True]


# ── 2. cron is the ONE documented exclusion — strip-falsify the filter itself ─


@pytest.mark.asyncio
async def test_cron_excluded_from_family_gate(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: ``cron`` — the one genuinely side-effecting seam (mutates the
    global scheduler) — is never invoked by ``refresh_config_projections()``.
    Strip-falsify: calling the underlying ``HotReloader.apply_all()`` WITHOUT the
    exclusion (the pre-#3097 shape a regression could reintroduce) DOES invoke
    ``cron`` — proving the exclusion in ``refresh_config_projections`` is an
    active filter, not a no-op that happens to never see cron for some other
    reason."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    session = Session(
        agent_name="a", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    result = await session.refresh_config_projections()
    assert "cron" not in result["invoked"]

    # Strip-falsify: the same underlying apply, without the cron exclusion.
    unfiltered = await session._hot_reloader.apply_all(exclude=frozenset())
    assert "cron" in unfiltered["invoked"], (
        "removing the exclude set must make cron fire — proving "
        "refresh_config_projections's exclude={'cron'} is load-bearing"
    )


# ── 3. spawn wiring: real disk config picked up by the family gate at spawn ──


_HELLO_PIPELINE = """
pipeline: hello
steps:
  - transform: {value: "'v1'", output: greeting}
"""


def _write_pipeline(tmp_path: Path, key: str = "ns") -> None:
    """Real on-disk pipeline install (mirrors ``test_2581_pipeline_hotreload.py``):
    a DSL file + a ``.reyn/config/pipelines.yaml`` entry declaring it. Unlike
    ``hook_state()`` (which re-reads disk live on EVERY call, making it useless as
    an observable of whether a hot-reload seam actually fired),
    ``Session.pipeline_registry`` is a CACHED field only ever swapped by
    ``_reapply_pipelines`` — a valid observable of whether the seam fired."""
    d = tmp_path / "pipelines"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.yaml").write_text(_HELLO_PIPELINE, encoding="utf-8")
    cfg_path = tmp_path / ".reyn" / "config" / "pipelines.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.dump({"pipelines": {"entries": {key: {"path": f"pipelines/{key}.yaml"}}}}),
        encoding="utf-8",
    )


def _write_skill(tmp_path: Path, name: str) -> None:
    p = tmp_path / ".reyn" / "config" / "skills.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump({"skills": {"entries": {name: {"path": "skills/" + name, "description": "d"}}}}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_spawn_session_recorded_refreshes_pipelines_and_skills(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: a real programmatic spawn (``spawn_session_recorded``) sees a
    pipeline AND a skill written to disk AFTER the registry's boot-time
    session_factory closure captured its (empty) snapshot — proving the family
    gate actually rebuilds these CACHED projections, not just bookkeeps an
    'invoked' flag. RED before #3097 (and before this PR's fold-in): the spawned
    session would inherit the factory's empty pipelines/skills snapshot
    forever."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("worker")  # pre-existing main session, unaffected by later writes

    _write_pipeline(tmp_path)
    _write_skill(tmp_path, "post_spawn_skill")

    sid = await reg.spawn_session_recorded(
        "worker", mode="ephemeral", presentation_consumer=None, intervention_bridge=None,
    )
    spawned = reg._peek_session("worker", sid)
    assert spawned is not None

    assert spawned.pipeline_registry.get("ns.hello") is not None

    skill_names = {s.name for s in (spawned._router_host.get_available_skills() or [])}
    assert "post_spawn_skill" in skill_names


# ── 4. recovery re-wake NEVER fires the family gate (snapshot fidelity) ──────


@pytest.mark.asyncio
async def test_recovery_shaped_spawn_does_not_refresh_config_projections(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the recovery invariant — crash-recovery re-wake
    (``AgentRegistry.restore_all`` / ``_rewake_pipeline_runs``) calls the
    LOWER-LEVEL ``AgentRegistry.spawn_session`` directly (never
    ``spawn_session_recorded``, the sole caller of ``refresh_config_projections``).
    This test exercises that EXACT call shape and confirms a pipeline installed to
    disk AFTER the registry booted is NOT picked up on the recovered session's
    (cached, seam-only-swapped) ``pipeline_registry`` — the re-created session
    keeps its factory-time (pre-crash-shaped) snapshot, never silently
    overwritten with CURRENT on-disk config (snapshot fidelity).

    Strip-falsify: the SAME setup via ``spawn_session_recorded`` instead (the
    non-recovery path) DOES pick it up — proving the family gate itself works
    (the recovery test isn't just green because the mechanism is broken
    everywhere) and that recovery's non-refresh is a real property of WHICH call
    it uses, not an accident."""
    from reyn.core.pipeline.registry import PipelineNotFoundError

    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("worker")

    _write_pipeline(tmp_path, key="recov")

    # The exact recovery-shaped call (mirrors registry.py:1176/1275 verbatim —
    # spawn_session, NOT spawn_session_recorded).
    routing = ReviewedNA("runtime/registry.py::restore_all")
    recovery_sid = reg.spawn_session(
        "worker", sid="recovered-1",
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    recovered = reg._peek_session("worker", recovery_sid)
    assert recovered is not None
    with pytest.raises(PipelineNotFoundError):
        recovered.pipeline_registry.get("recov.hello")

    # Strip-falsify: the SAME pipeline, via the real (non-recovery) spawn path.
    normal_sid = await reg.spawn_session_recorded(
        "worker", mode="ephemeral", presentation_consumer=None, intervention_bridge=None,
    )
    normal = reg._peek_session("worker", normal_sid)
    assert normal is not None
    assert normal.pipeline_registry.get("recov.hello") is not None, (
        "sanity/strip-falsify: the SAME pipeline IS picked up via the real "
        "spawn_session_recorded path — proving the recovery test above is not "
        "vacuously green (the mechanism does work; recovery specifically "
        "avoids it)"
    )


# ── 5. security: visibility_override is narrow-only, spawn-time envelope authorized ──


def _bind_topology(tmp_path: Path, *, member: str, profile: str, body: str) -> None:
    td = tmp_path / ".reyn" / "topologies"
    td.mkdir(parents=True, exist_ok=True)
    (td / "t.yaml").write_text(
        f"name: t\nkind: network\nmembers: [{member}, peer]\nprofiles:\n  {member}: {profile}\n",
        encoding="utf-8",
    )
    pd = tmp_path / ".reyn" / "capability_profiles"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / f"{profile}.yaml").write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_spawn_visibility_override_seam_resolves_authorized_envelope_narrow_only(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: security-core — ``_reapply_visibility_override`` fires at spawn
    (family-gate member) and re-resolves the SPAWNED session's envelope from its
    CURRENT authorized base (topology binding). The topology-denied tool must be
    denied on the spawned session's live ``contextual_permission`` — proving the
    seam actually ran (not just bookkept 'invoked') — and toggling the tool's
    session visibility back ON afterward must NOT re-grant it (restrict-only
    compose: visible ⊆ authorized, no escalation past the topology floor is
    possible via the seam or the toggle)."""
    from reyn.security.permissions.effective import tool_contextually_denied

    monkeypatch.chdir(tmp_path)
    _bind_topology(
        tmp_path, member="worker", profile="role",
        body="name: role\ntool_deny: [delete_file]\n",
    )
    reg = _make_registry(tmp_path)
    reg.get_or_load("worker")

    sid = await reg.spawn_session_recorded(
        "worker", mode="ephemeral", presentation_consumer=None, intervention_bridge=None,
    )
    spawned = reg._peek_session("worker", sid)
    assert spawned is not None

    # The family gate's visibility_override seam re-resolved the spawned
    # session's OWN envelope from the topology binding — the denial is live.
    assert tool_contextually_denied(spawned.contextual_permission, "delete_file"), (
        "the spawned session's contextual_permission must reflect the "
        "topology-bound denial — proving the visibility_override seam actually "
        "fired at spawn, not just appeared in the 'invoked' bookkeeping"
    )

    # Attempting to re-grant it via the session visibility toggle must be a
    # no-op past the authorized envelope (restrict-only ∩ compose).
    spawned.set_capability_visible("tool", "delete_file", True)
    assert tool_contextually_denied(spawned.contextual_permission, "delete_file"), (
        "toggling a topology-denied tool's session visibility back ON must "
        "NOT re-grant it — the compose is restrict-only, so no path through "
        "the visibility_override seam or the toggle can escalate past the "
        "authorized envelope"
    )
