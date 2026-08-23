"""Tier 2: #5217 — ``AgentRegistry.get_or_load`` publishes a session to its
own map ONLY after construction is COMPLETE, never before.

Architect (issuecomment-5385481839, during #5215's own review): the
accidental safety #5203 measured — a session was visible in the registry's
own map for the 3 lines between ``_store_session`` and ``restore_state``,
and nothing broke ONLY because the one field #4983's off-thread readers
actually consumed (``Session.history``) happens to already be hydrated
before that window opens — is replaced here by a STRUCTURAL guarantee: any
thread that finds a session in the registry's map at all now finds a
FULLY-BUILT one (toggles loaded, pending state restored), or finds
NOTHING. Never a half-built one.

#5215 tried to close the SAME gap from the reader's side (an owner-thread
guard on the 7 reads) — withdrawn once found to be topology-dependent (the
web server structurally cannot honor a single-owner-thread invariant for
sync endpoints). This is the fix architect actually prescribed: move the
WRITER's own publish point instead, so no reader-side guard is needed at
all.

Real ``AgentRegistry`` + a real ``Session`` (``make_session``) + a real
second OS thread gating ``Session.load_persisted_toggles`` with a
``threading.Event`` (a controllable seam, never a sleep — CLAUDE.md's
floor/ceiling rule) — no mocks.
"""
from __future__ import annotations

import threading

import pytest

from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _registry(tmp_path) -> AgentRegistry:
    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


def test_a_session_under_construction_is_never_visible_half_built(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #5217's own falsifier. Gates ``Session.load_persisted_
    toggles`` (the FIRST of the 2 steps #5217 moved ``_store_session``
    past) mid-call on a real second OS thread; while it is held open, the
    constructing thread has not returned from ``get_or_load`` yet — the
    registry's own map must show NOTHING for this name, never a
    partially-built ``Session``. Reverting #5217 (moving ``_store_session``
    back to BEFORE these 2 steps, #5215's own pre-fix shape) turns this
    red: the gated read would then observe a real, live (but half-built)
    ``Session`` instance instead of ``None``."""
    reg = _registry(tmp_path)
    real_load = Session.load_persisted_toggles
    started = threading.Event()
    release = threading.Event()

    def _gated_load_persisted_toggles(self) -> None:
        started.set()
        # #5216-adjacent note, CLAUDE.md floor/ceiling rule (architect
        # issuecomment-5385725693): wait on the condition UNBOUNDED — no
        # inline timeout=. CI's own --timeout is the sole kill switch; an
        # inline ceiling here would risk a false RED (and a misleading
        # message) on a genuinely slow machine, not a real defect.
        release.wait()
        real_load(self)

    monkeypatch.setattr(
        Session, "load_persisted_toggles", _gated_load_persisted_toggles,
    )

    caught: "list[BaseException | None]" = [None]

    def _construct() -> None:
        try:
            reg.get_or_load("alpha")
        except BaseException as e:  # noqa: BLE001 — inspected on the main thread
            caught[0] = e

    t = threading.Thread(target=_construct)
    t.start()
    try:
        started.wait()
        # The constructing thread is now PAUSED inside load_persisted_
        # toggles — get_or_load has not returned, so #5217's own claim is:
        # nothing is published yet. get_session (FP-0043 Stage 3) is the
        # SUPPORTED public accessor for this — not a private reach-in.
        assert reg.get_session("alpha") is None, (
            "a session under construction must not be visible in the "
            "registry's own map yet — #5217's whole point"
        )
    finally:
        release.set()
        t.join()
    assert caught[0] is None, f"construction must not raise, got: {caught[0]!r}"

    # Now that construction has completed, the session IS visible via the
    # same public accessor.
    assert reg.get_session("alpha") is not None, (
        "construction must publish the session once complete"
    )


@pytest.mark.asyncio
async def test_get_or_load_still_returns_the_fully_built_session(tmp_path) -> None:
    """Tier 2: accept-side, no gating — the ordinary (single-thread,
    ungated) path must still work byte-identically: ``get_or_load``
    returns a real, usable ``Session``, same-thread, same call shape as
    before #5217."""
    reg = _registry(tmp_path)
    session = reg.get_or_load("alpha")
    assert session is not None
    assert reg.get_session("alpha") is session, (
        "the returned session must be the SAME object now stored in the registry"
    )

    # A second get_or_load for the same name is a cache hit (byte-identical
    # to pre-#5217): no re-construction, no re-publish.
    session_again = reg.get_or_load("alpha")
    assert session_again is session
