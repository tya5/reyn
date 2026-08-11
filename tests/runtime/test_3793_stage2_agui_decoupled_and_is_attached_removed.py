"""Tier 2: #3793 stage 2 (ADR-0039 D4 conformance) — AG-UI stops sharing the
registry's ``AttachedConnection``, and ``Session.is_attached`` is removed.

Stage 1 (#3809, merged) introduced ``AttachedConnection`` but left BOTH the
local (TUI) and remote (AG-UI) call sites pointing at the SAME shared
instance — attaching via one still flipped what the other observed. Stage 2
closes that gap for the piece that actually mattered: AG-UI never read
``registry.attached_name``/``attached_session``/``attach_failed`` at all
(measured: zero references in ``interfaces/transport/agui/``) — the only
thing its 5 ``registry.attach(agent_name)`` call sites needed was the BOOT
side effect (session running + forwarder). Stage 2 redirects those 5 sites to
``registry.ensure_running(agent_name)`` (generalized to accept an explicit
``sid``, default-preserving for every pre-existing caller) — a method that
boots without touching the registry's own connection at all.

Stage 2 also removes ``Session.is_attached`` (a second, single-bool
representation of "attached-ness" that could not express per-connection
focus once N:N applies) and its 5 manual-sync sites in ``registry.py`` — the
"nobody is watching, drop status/trace" behaviour it approximated is already
achieved structurally by ``OutboxHub._fanout``'s genuine zero-subscriber
no-op.

This file covers:
- N:N witness: an AG-UI-shaped boot (``ensure_running``) does not touch
  ``registry.attached_name``/``attached_session`` — the TUI's own attach
  state is unaffected by it.
- ``addressed`` witness: an attached-but-not-active session's intervention
  is still answerable by id, independent of ``active`` (unaffected by this
  stage — pinned here as a regression guard, per the issue's own Test plan).
- ``is_attached``-removal witness: a session's outbox does not grow
  unboundedly once booted via the production path (the forwarder subscriber
  ``ensure_running`` starts keeps the hub's source queue drained regardless
  of registry focus), even though the "nobody attached" gate at the source
  is gone.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _registry(tmp_path):
    shared = BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=shared,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    reg.create("beta")
    return reg


# ---------------------------------------------------------------------------
# N:N witness — the actual behaviour change this stage exists to make
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agui_shaped_boot_does_not_touch_registry_focus(tmp_path) -> None:
    """Tier 2: #3793 stage 2 — an AG-UI-shaped call (``ensure_running``,
    mirroring ``agui/endpoint.py``'s 5 call sites) boots a session WITHOUT
    changing ``registry.attached_name``/``attached_session`` — the TUI's own
    focus stays exactly where it was (or unset, if nothing ever attached).

    Falsification (performed during review): adding
    ``self._connection.switch(key)`` to ``AgentRegistry.ensure_running``
    (as ``attach()`` itself does) makes this test go RED — ``attached_name``
    reads ``"beta"`` after the AG-UI-shaped call instead of staying
    ``"alpha"``.
    """
    reg = _registry(tmp_path)

    await reg.attach("alpha")  # TUI attaches to alpha
    assert reg.attached_name == "alpha"

    # AG-UI-shaped boot of a DIFFERENT session — must not touch focus.
    session = await reg.ensure_running("beta")
    assert session is not None

    assert reg.attached_name == "alpha", (
        "an AG-UI-shaped boot of a different session must not move the "
        "registry's own (TUI-facing) focus pointer"
    )
    assert reg.attached_session().agent_name == "alpha"


@pytest.mark.asyncio
async def test_agui_shaped_boot_of_the_same_agent_also_does_not_touch_focus(tmp_path) -> None:
    """Tier 2: #3793 stage 2 — even when the AG-UI-shaped call targets the
    SAME agent the TUI is attached to, focus is untouched (this is the case
    that most resembled the old shared-pointer coupling — both point at
    "alpha" — so it is the sharpest test that nothing implicitly re-syncs
    them)."""
    reg = _registry(tmp_path)

    await reg.attach("alpha")
    assert reg.attached_name == "alpha"

    session = await reg.ensure_running("alpha")
    assert session is not None
    assert reg.attached_name == "alpha"  # unchanged, not merely "still alpha by luck"


@pytest.mark.asyncio
async def test_ensure_running_omitted_sid_still_resolves_to_the_default_session(tmp_path) -> None:
    """Tier 2: #3793 stage 2 — ``ensure_running``'s new ``sid`` parameter
    defaults to ``_DEFAULT_SID``, so every pre-existing caller that omits it
    (agent-to-agent messaging, per its own docstring) keeps booting the
    default session exactly as before. Checked via a PUBLIC behavioral
    consequence rather than the private ``_tasks``/``_forward_tasks`` dicts:
    ``attach_session(name, _DEFAULT_SID)`` requires the target to ALREADY be
    booted (it raises ``KeyError`` otherwise, per its own docstring) — so it
    succeeding is proof ``ensure_running("alpha")`` (no sid) really booted
    the ``_DEFAULT_SID`` session, not some other one.

    Falsification (performed during review): removing the ``= _DEFAULT_SID``
    default (making ``sid`` required) makes every existing
    ``registry.ensure_running(name)`` call site raise ``TypeError`` — this
    test's own bare call would go RED with that error.
    """
    reg = _registry(tmp_path)
    session = await reg.ensure_running("alpha")
    assert session is not None

    resolved = await reg.attach_session("alpha", _DEFAULT_SID)
    assert resolved is session, (
        "ensure_running('alpha') with sid omitted must have booted the same "
        "session attach_session('alpha', _DEFAULT_SID) finds"
    )


@pytest.mark.asyncio
async def test_real_agui_endpoint_boot_does_not_touch_registry_focus(tmp_path, monkeypatch) -> None:
    """Tier 2: #3793 stage 2 — production-name witness. Drives the REAL
    ``agui/endpoint.py`` HTTP surface (not a hand-rolled fake, not a direct
    call to ``ensure_running``) and confirms a POST that boots/uses a
    session leaves ``registry.attached_name`` exactly where the TUI left it.

    This is the gap PR #3807's review closed for #3792's seam: a test that
    only exercises the underlying primitive (``ensure_running`` directly, as
    the tests above do) does not prove the REAL endpoint code calls it
    instead of ``attach()`` — renaming/reverting one of the 5 call sites in
    ``endpoint.py`` back to ``attach(agent_name)`` would leave the primitive
    -level tests green while this one goes RED.

    Falsification (performed during review): reverting
    ``agui/endpoint.py``'s ``agui_submit`` call site (the one this test
    actually exercises, via ``type: "user_message"``) back to
    ``registry.attach(agent_name)`` makes this test go RED —
    ``attached_name`` reads ``"beta"`` (the POST's target agent) instead of
    staying ``"alpha"``.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from reyn.core.events.state_log import StateLog
    from reyn.interfaces.transport.agui import endpoint as endpoint_mod
    from reyn.interfaces.transport.agui.endpoint import router
    from reyn.interfaces.web.auth import AuthContext
    from reyn.runtime.registry import AgentRegistry
    from tests._support.agent_session import make_session

    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None):
        return make_session(
            agent_name=profile.name, state_log=state_log, non_interactive=True,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    reg.create("alpha")
    reg.create("beta")
    await reg.attach("alpha")  # TUI is attached to alpha
    assert reg.attached_name == "alpha"

    app = FastAPI()
    app.include_router(router)
    app.state.auth = AuthContext(token="s3cret", require_token=True)
    monkeypatch.setattr(endpoint_mod, "get_registry", lambda: reg)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/agui/chat/beta?token=s3cret",
            json={"type": "user_message", "text": "hi from a remote client"},
        )
        assert resp.status_code == 200, f"POST to beta failed: {resp.status_code} {resp.text}"

    assert reg.attached_name == "alpha", (
        "a real AG-UI POST targeting a DIFFERENT agent (beta) must not move "
        "the TUI's own registry-level focus away from alpha"
    )


# ---------------------------------------------------------------------------
# addressed witness — unaffected by this stage, pinned as a regression guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_addressed_delivery_does_not_depend_on_active(tmp_path) -> None:
    """Tier 2: #3793 — ``addressed`` input (id-carrying: an intervention
    answer by id) reaches an attached-but-not-active session — unaffected by
    ``active``. Pinned per the issue's own Test plan (a regression guard,
    not new behaviour this stage adds): switching TUI focus away from a
    session must not prevent answering that session's OWN pending
    intervention by id.
    """
    reg = _registry(tmp_path)
    beta = await reg.ensure_running("beta")  # booted, never made "active"
    await reg.attach("alpha")  # TUI's active session is alpha, not beta

    assert reg.attached_name == "alpha"

    from reyn.user_intervention import InterventionAnswer, UserIntervention

    iv = UserIntervention(
        id="iv-1", kind="ask_user", prompt="proceed?", detail="", choices=[],
        origin_channel_id="test",
    )
    beta._interventions.register_listener("test")
    task = asyncio.ensure_future(beta.handle_intervention(iv))
    await asyncio.sleep(0)  # let handle_intervention register the pending future
    answered = await beta.answer_intervention_by_id("iv-1", "yes")
    assert answered is True, (
        "answering beta's OWN pending intervention by id must succeed even "
        "though beta is not the TUI's active session"
    )
    result: InterventionAnswer = await task
    assert result.text == "yes"


# ---------------------------------------------------------------------------
# is_attached-removal witness — the source queue does not grow unboundedly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_queue_does_not_grow_unboundedly_without_tui_focus(tmp_path) -> None:
    """Tier 2: #3793 stage 2 — with the ``Session.is_attached`` gate replaced
    by ``outbox_hub.has_subscribers()``, a status/trace message emitted while
    the forwarder IS subscribed (registry focus is irrelevant to the hub)
    still reaches and drains through ``session.outbox`` (the hub's source
    queue) — the forwarder's own hub subscription is what keeps the queue
    drained here, same as before the gate's re-derivation.

    Falsification (performed during review): starting a session WITHOUT
    booting the forwarder (calling ``get_or_load`` alone, skipping
    ``ensure_running``'s task-boot) and repeating the same puts makes
    ``session.outbox.qsize()`` grow with every message — confirming the
    forwarder's hub subscription is what bounds this in the real boot path,
    not merely the gate.
    """
    reg = _registry(tmp_path)
    session = await reg.ensure_running("alpha")  # boots the forwarder too

    for i in range(20):
        session._put_outbox_nowait(
            OutboxMessage(kind="status", text=f"status {i}")
        )
        # Let the hub's drain task actually run and fan the message out.
        await asyncio.sleep(0)

    assert session.outbox.qsize() == 0, (
        "the forwarder's hub subscription must keep draining session.outbox "
        f"regardless of TUI focus; got qsize={session.outbox.qsize()}"
    )


@pytest.mark.asyncio
async def test_source_queue_does_not_grow_for_never_subscribed_session(tmp_path) -> None:
    """Tier 2: #3793 stage 2 review follow-up (blocking finding on #3813) —
    a session booted via ``ensure_session_running`` (FP-0043 4b-2: no
    forwarder, used by persistent ``cron:``/``webhook:`` sessions and other
    fire-and-forget drivers) is never subscribed to by anything. Before this
    stage, ``Session.is_attached`` defaulted ``False`` for such a session, so
    ``status``/``trace`` were dropped at the source regardless. After the
    field's removal, EVERY put reached ``session.outbox`` unconditionally —
    and since nothing ever calls ``outbox_hub.subscribe()``, the hub's drain
    task never starts, so the source queue would grow without bound for the
    session's entire (potentially process-lifetime) duration.

    The fix: ``_put_outbox_nowait`` now gates status/trace on
    ``outbox_hub.has_subscribers()`` directly (not on a drain task already
    running), so a never-subscribed session drops them at emission exactly
    as the old ``is_attached``-False default did — this test is the N:N-model
    equivalent of that old default, derived from actual subscriber state.

    Falsification (performed for real): temporarily changing the gate back to
    unconditional ``self.outbox.put_nowait(msg)`` (no ``has_subscribers()``
    check) makes ``session.outbox.qsize()`` grow to 20 after this same loop —
    confirmed RED for the exact predicted reason, then restored to GREEN.
    """
    reg = _registry(tmp_path)
    # Mirrors reyn.hooks.ingress.CronIngressAdapter.resolve_session exactly:
    # a persistent cron:<job_name> session, resolved then booted with no
    # forwarder — the real shape a never-subscribed session takes in prod.
    session = reg.resolve_session("alpha", "cron", "nightly")
    registry_running = reg.ensure_session_running("alpha", "cron:nightly")
    assert registry_running is session

    for i in range(20):
        session._put_outbox_nowait(
            OutboxMessage(kind="status", text=f"status {i}")
        )
        await asyncio.sleep(0)

    assert not session.outbox_hub.has_subscribers()
    assert session.outbox.qsize() == 0, (
        "a never-subscribed (ensure_session_running-booted) session's outbox "
        f"must not grow from transient status/trace puts; got "
        f"qsize={session.outbox.qsize()}"
    )
