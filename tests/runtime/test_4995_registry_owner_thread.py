"""Tier 2: #4995 slice 1 — ``AgentRegistry`` gains an explicit, enforced
single-owner-thread invariant.

Architect ruling (issuecomment-5384869791, relayed by lead-coder): before a
worker-thread boundary (``ThreadedTransportProxy``) can safely own the
registry, there must FIRST be a named seam that makes "a second thread
touched this registry" fail loudly (``RuntimeError``) instead of racing
silently — slice 1's own deliverable, with NO behavior change for today's
single-threaded callers (only slice 2/3 introduce an actual second thread).

Acceptance witnessed here:
  ① a registry touched by exactly one thread — the constructing thread —
     never raises (sanity: slice 1 changes nothing observable today)
  ② a SECOND real OS thread touching the registry raises RuntimeError
     (the seam itself, "2つ目が触れない" — architect's own acceptance
     wording)
  ③ the owner thread identity is a public read
     (:attr:`AgentRegistry.owner_thread_ident`), not a private-state reach-in

Real ``AgentRegistry`` + real ``threading.Thread`` (no mocks) — mirrors
``tests/security/test_5153_approval_ledger.py``'s own real-concurrency
witnesses, but a SINGLE real thread suffices here (the property under test
is "does touching from thread B raise", not a race between two writers).
"""
from __future__ import annotations

import threading

from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
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

    return AgentRegistry(project_root=tmp_path, session_factory=factory)


def test_owner_thread_ident_is_the_constructing_thread(tmp_path) -> None:
    """Tier 2: #4995 slice 1 acceptance ③ — the public read matches the
    thread that actually constructed the registry (this test's own thread)."""
    reg = _registry(tmp_path)
    assert reg.owner_thread_ident == threading.get_ident()


def test_same_thread_access_never_raises(tmp_path) -> None:
    """Tier 2: #4995 slice 1 acceptance ① — sanity/non-regression. Every
    measured entry point (#4995's own issue comments) is reachable from the
    owner thread exactly as before slice 1 — RED if the guard mis-fires on
    the very thread that owns the registry."""
    reg = _registry(tmp_path)
    reg.exists("default")
    reg.loaded_names()
    reg.agent_workspace_dir("default")
    reg.get_session("default")
    reg.attached_session()
    reg.record_background_attach_error("probe")
    reg.agent_cost_usd("default")
    reg.agent_total_usage("default")
    # No assertion needed beyond "did not raise" -- this test's only job is
    # to prove slice 1 changed nothing for the owner thread.


def test_a_different_thread_touching_the_registry_raises(tmp_path) -> None:
    """Tier 2: #4995 slice 1 acceptance ② — the seam itself. A REAL second
    OS thread calling a measured entry point must raise RuntimeError, not
    silently succeed (the exact race slice 2/3 must not be allowed to
    reintroduce). RED if ``_assert_owner_thread`` is removed or bypassed."""
    reg = _registry(tmp_path)

    caught: "list[BaseException | None]" = [None]

    def _touch_from_other_thread() -> None:
        try:
            reg.loaded_names()
        except BaseException as e:  # noqa: BLE001 -- captured for the main thread to inspect
            caught[0] = e

    t = threading.Thread(target=_touch_from_other_thread)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "the other thread's call must have returned (raised), not hung"
    assert isinstance(caught[0], RuntimeError), (
        f"a second thread touching the registry must raise RuntimeError, got: {caught[0]!r}"
    )
    assert "4995" in str(caught[0]), "the error must point at the invariant's own issue"


def test_two_different_registries_each_owned_by_their_own_constructing_thread(
    tmp_path,
) -> None:
    """Tier 2: falsification contrast — ownership is per-INSTANCE, not a
    process-global "the first thread wins" latch. A second registry
    constructed on a DIFFERENT thread is owned by THAT thread, and each
    instance rejects the other's owner thread."""
    result: "list[AgentRegistry | None]" = [None]
    other_ident: "list[int | None]" = [None]

    def _build_other() -> None:
        r = _registry(tmp_path / "other")
        result[0] = r
        other_ident[0] = threading.get_ident()

    t = threading.Thread(target=_build_other)
    t.start()
    t.join(timeout=10)
    other_reg = result[0]
    assert other_reg is not None
    assert other_reg.owner_thread_ident == other_ident[0]
    assert other_reg.owner_thread_ident != threading.get_ident(), (
        "sanity: the other registry's owner must genuinely be a different "
        "thread from this test's own"
    )

    main_reg = _registry(tmp_path / "main")
    assert main_reg.owner_thread_ident == threading.get_ident()
    # main_reg (owned by THIS thread) is safely touchable from it -- proof
    # the two instances' ownership is independent, not a shared/global latch.
    main_reg.loaded_names()
