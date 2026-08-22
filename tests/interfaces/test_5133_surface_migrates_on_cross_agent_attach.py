"""Tier 2: #5133 — a cross-agent ``/attach`` migrates this connection's
``SurfaceManager`` registration, not just #5129's own "which agent name to
ask" fact.

#5129 fixed `agui_submit`/`agui_seize` to resolve `agent_name` from the
connection instead of trusting their own URL path param. That closed the
plain "input reaches the wrong agent" symptom, but left a SEPARATE holder
untouched: `SurfaceRegistry.get(agent_name)` only returns a manager for an
agent that has had an actual connection `attach()`ed to it (done only by
`agui_events` at SSE-connect time) — a cross-agent `/attach` never
re-registered this connection there, so `/seize` 409ed (unknown surface) and
a HITL answer's `is_active_driver` check read whichever OTHER connection
(if any) was already registered on the target.

Architect ruling (issuecomment-5383089515, #5133): no new semantics — a
cross-agent `/attach` IS `SurfaceManager.attach(target)` + `detach(old)`,
the SAME two primitives `agui_events`' own connect/disconnect already use
(mirrors #5118's "one mechanism, two producers, not two mechanisms").
ARRIVAL before DEPARTURE (never a window where this connection belongs to
no manager). The driver token is NEVER carried across — `attach()`'s own
existing default (first surface on an empty manager takes it; an existing
holder keeps it) already IS the right rule, so calling it unchanged is the
fix, not a special case. The old agent's fail-close grace treats this
exactly like an ordinary disconnect (arms only if this was its last
surface); the target's treats arrival exactly like an ordinary connect
(disarms an already-armed grace) — no "migration is special" branching on
either side, architect's own explicit warning against giving the same end
state two arrival paths.

4 acceptance witnesses (architect's own ruling, each separate, never
derived from a symptom):
1. the target already has another driver -> the migrated connection does
   NOT become driver, but an explicit /seize afterwards succeeds (ordinary
   symmetric contention, not automatically stolen).
2. the old agent still has another connection watching it -> its
   fail-close grace is NOT armed by the migration.
3. the old agent's LAST connection migrates away -> its grace arms
   IDENTICALLY to an ordinary disconnect (measured in the SAME test, same
   grace_seconds, same SurfaceManager class, against a real plain
   disconnect for comparison — not just "does something get armed").
4. the target's ALREADY-armed grace (its own last surface had already left)
   is disarmed by the arriving connection, exactly like any ordinary
   connect.

Chains the real wire pieces (real ASGI POST -> real SurfaceManager/
SurfaceRegistry -> real Session), mirroring
test_5129_submit_follows_connection_agent.py's own harness. No mocks.
"""
from __future__ import annotations

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.transport.agui.endpoint import _SessionFrameSource
from reyn.interfaces.transport.agui.surface import monotonic, surface_registry
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _two_agent_registry(tmp_path, monkeypatch, origin_name: str, target_name: str):
    """*origin_name*/*target_name* are per-TEST-unique (never "default" /
    "coder-smith") -- ``surface_registry()`` is a process-global singleton
    keyed by agent name (module-level ``_REGISTRY``, ``surface.py``), so a
    literal shared across test functions in this file would carry leftover
    surfaces from an earlier test into this one, exactly the false-red/
    false-green trap this file's own tests exist to avoid in the endpoint
    under test. Each test picks its own two names so this file's tests
    cannot desync each other via that shared singleton."""
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None):
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    reg.create(origin_name)
    reg.create(target_name)
    return reg


def _build_app(reg, monkeypatch):
    from fastapi import FastAPI

    from reyn.interfaces.transport.agui import endpoint as endpoint_mod
    from reyn.interfaces.transport.agui.endpoint import router
    from reyn.interfaces.web.auth import AuthContext

    app = FastAPI()
    app.include_router(router)
    app.state.auth = AuthContext(token="s3cret", require_token=True)
    monkeypatch.setattr(endpoint_mod, "get_registry", lambda: reg)
    return app


async def _post(app, path: str, payload: dict):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        return await client.post(path, json=payload)


def _authorized(user_id) -> bool:
    return bool(user_id)


@pytest.mark.asyncio
async def test_migrating_into_a_manager_with_an_existing_driver_does_not_steal_it_but_seize_still_works(
    tmp_path, monkeypatch,
):
    """Tier 2: acceptance ① — architect's own explicit "arrival competes
    normally, never seizes"."""
    origin, target = "5133-t1-origin", "5133-t1-target"
    reg = _two_agent_registry(tmp_path, monkeypatch, origin, target)
    origin_session = await reg.attach(origin)
    app = _build_app(reg, monkeypatch)

    # connB: a SECOND, real connection already directly attached to the
    # TARGET agent BEFORE connA migrates in -- it takes the driver token as
    # any first surface would (SurfaceManager.attach's own existing rule).
    mgr_target = surface_registry().for_agent(target, authorized=_authorized)
    mgr_target.attach("conn-B", "userB", monotonic())
    assert mgr_target.is_active_driver("conn-B")

    conn_a = "conn-A-migrating"
    mgr_origin = surface_registry().for_agent(origin, authorized=_authorized)
    mgr_origin.attach(conn_a, "userA", monotonic())

    source = _SessionFrameSource(origin_session, registry=reg, agent_name=origin)
    source.listen_for_retarget(conn_a)
    source.start()

    try:
        resp = await _post(
            app, f"/agui/chat/{origin}?connection_id={conn_a}&token=s3cret",
            {"type": "attach_request", "agent_name": target},
        )
        assert resp.json().get("attached") is True

        # ① connA did NOT steal the token just by arriving.
        assert mgr_target.is_active_driver("conn-B")
        assert not mgr_target.is_active_driver(conn_a)

        # An explicit /seize afterwards is the ordinary, un-special-cased
        # contention path -- it succeeds because connA is now a genuinely
        # registered surface of the target's own manager.
        seize_resp = await _post(
            app, f"/agui/chat/{origin}/seize?connection_id={conn_a}&token=s3cret", {},
        )
        assert seize_resp.status_code == 200
        assert seize_resp.json().get("seized") is True
        assert mgr_target.is_active_driver(conn_a)
    finally:
        source.close()


@pytest.mark.asyncio
async def test_migration_does_not_arm_the_old_agents_grace_while_another_connection_remains(
    tmp_path, monkeypatch,
):
    """Tier 2: acceptance ② — the old agent still has a live watcher, so its
    fail-close grace must NOT arm just because ONE connection left."""
    origin, target = "5133-t2-origin", "5133-t2-target"
    reg = _two_agent_registry(tmp_path, monkeypatch, origin, target)
    origin_session = await reg.attach(origin)
    app = _build_app(reg, monkeypatch)

    conn_a = "conn-A-migrating"
    conn_c = "conn-C-stays-on-origin"
    mgr_origin = surface_registry().for_agent(origin, authorized=_authorized)
    mgr_origin.attach(conn_a, "userA", monotonic())
    mgr_origin.attach(conn_c, "userC", monotonic())

    source = _SessionFrameSource(origin_session, registry=reg, agent_name=origin)
    source.listen_for_retarget(conn_a)
    source.start()

    try:
        resp = await _post(
            app, f"/agui/chat/{origin}?connection_id={conn_a}&token=s3cret",
            {"type": "attach_request", "agent_name": target},
        )
        assert resp.json().get("attached") is True

        assert mgr_origin.surface_count() == 1, (
            f"expected conn_a to have actually LEFT (only conn_c remaining), "
            f"got {mgr_origin.surface_count()} surfaces -- a migration that "
            f"never detaches conn_a would ALSO leave has_surfaces()/"
            f"should_fail_close() looking exactly like this, so the count "
            f"is the witness that departure genuinely happened, not just "
            f"that grace happens not to be armed"
        )
        assert not mgr_origin.should_fail_close(monotonic() + mgr_origin.grace_seconds + 1.0), (
            "the grace window armed even though another connection still "
            "watches this agent -- a migration must not behave differently "
            "from any other single-surface detach"
        )
    finally:
        source.close()


@pytest.mark.asyncio
async def test_migration_arms_the_old_agents_grace_identically_to_an_ordinary_disconnect(
    tmp_path, monkeypatch,
):
    """Tier 2: acceptance ③ — measured against a REAL plain disconnect in
    the SAME test (architect: "if you claim 'the same', measure the
    disconnect side with the same test too"), same grace_seconds, same
    SurfaceManager class."""
    origin, target = "5133-t3-origin", "5133-t3-target"
    reg = _two_agent_registry(tmp_path, monkeypatch, origin, target)
    origin_session = await reg.attach(origin)
    app = _build_app(reg, monkeypatch)

    conn_a = "conn-A-migrating"
    mgr_origin = surface_registry().for_agent(origin, authorized=_authorized)
    mgr_origin.attach(conn_a, "userA", monotonic())

    source = _SessionFrameSource(origin_session, registry=reg, agent_name=origin)
    source.listen_for_retarget(conn_a)
    source.start()

    try:
        resp = await _post(
            app, f"/agui/chat/{origin}?connection_id={conn_a}&token=s3cret",
            {"type": "attach_request", "agent_name": target},
        )
        assert resp.json().get("attached") is True

        assert not mgr_origin.has_surfaces(), "conn_a was the only surface"
        past_grace = monotonic() + mgr_origin.grace_seconds + 1.0
        assert mgr_origin.should_fail_close(past_grace), (
            "migrating away the LAST surface must arm the grace window "
            "exactly as an ordinary disconnect would"
        )
    finally:
        source.close()

    # The comparison: an UNRELATED agent's manager, one surface, an
    # ORDINARY plain detach() (no attach_request, no /attach at all) --
    # same grace_seconds (the manager's own default), same class, same
    # `past_grace` offset. If migration's arming were somehow a DIFFERENT
    # code path with different timing, this is where a divergence would
    # show up.
    mgr_plain = surface_registry().for_agent("5133-t3-plain-disconnect-control", authorized=_authorized)
    mgr_plain.attach("conn-D-plain", "userD", monotonic())
    mgr_plain.detach("conn-D-plain", monotonic())
    assert not mgr_plain.has_surfaces()
    assert mgr_plain.should_fail_close(monotonic() + mgr_plain.grace_seconds + 1.0), (
        "sanity: an ordinary disconnect arms this exact same way -- "
        "migration's own arming above is not a special, different timer"
    )


@pytest.mark.asyncio
async def test_migration_disarms_the_targets_already_armed_grace(
    tmp_path, monkeypatch,
):
    """Tier 2: acceptance ④ — the target's grace was already armed (its
    own last surface had already left); the arriving connection disarms it
    exactly like any ordinary connect (SurfaceManager.attach's own existing
    `self._last_empty_at = None`). No real waiting: `now` values are
    supplied, never slept for."""
    origin, target = "5133-t4-origin", "5133-t4-target"
    reg = _two_agent_registry(tmp_path, monkeypatch, origin, target)
    origin_session = await reg.attach(origin)
    app = _build_app(reg, monkeypatch)

    mgr_target = surface_registry().for_agent(target, authorized=_authorized)
    mgr_target.attach("conn-D-left-already", "userD", monotonic())
    mgr_target.detach("conn-D-left-already", monotonic())
    armed_check_at = monotonic() + mgr_target.grace_seconds + 1.0
    assert mgr_target.should_fail_close(armed_check_at), (
        "setup sanity: the target's grace must be armed before the "
        "arrival this test is actually about"
    )

    conn_a = "conn-A-migrating"
    mgr_origin = surface_registry().for_agent(origin, authorized=_authorized)
    mgr_origin.attach(conn_a, "userA", monotonic())

    source = _SessionFrameSource(origin_session, registry=reg, agent_name=origin)
    source.listen_for_retarget(conn_a)
    source.start()

    try:
        resp = await _post(
            app, f"/agui/chat/{origin}?connection_id={conn_a}&token=s3cret",
            {"type": "attach_request", "agent_name": target},
        )
        assert resp.json().get("attached") is True

        assert not mgr_target.should_fail_close(armed_check_at), (
            "the arriving connection must disarm the target's already-"
            "armed grace, exactly as an ordinary new connect would"
        )
    finally:
        source.close()
