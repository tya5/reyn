"""Tier 2: #4995 slice 1 — ``AgentRegistry`` gains an explicit, enforced
single-owner-thread invariant, scoped to exactly the 4 methods that MUTATE
registry-owned state.

## History (read this before touching any test below)

Architect ruling (issuecomment-5384869791), CORRECTED after CI caught the
first version (issuecomment-5384963741, PR #5202): the guard broke a
legitimate feature — ``app.py``'s #4983 design deliberately read
conversation history off the event loop via ``asyncio.to_thread``, a real
second OS thread reaching 7 read-only registry methods through
``RegistryReadModel``. #5202 scoped the guard down to just the 4 mutators
(``attach``, ``restore_all``, ``resume_deferred_agents``,
``record_background_attach_error``) to stop breaking that (27 CI
failures, all the identical ``RuntimeError``). The 7 reads (``exists``,
``loaded_names``, ``agent_cost_usd``, ``agent_total_usage``,
``attached_session``, ``get_session``, ``agent_workspace_dir``) are NOT
asserted — see each one's own docstring.

#5203 tried to restore the guard onto the 7 reads too (architect
issuecomment-5385152402, reasoning that app.py's own hoist — see
``read_model.py``'s ``resolve_conversation_history_source``/
``conversation_history_from_source`` — closed the ONLY legitimate
off-thread reader). **WITHDRAWN by the same architect the same day**
(issuecomment-5385481839, PR #5215): CI caught 2 MORE legitimate
off-thread readers this restoration never enumerated — the web server's
A2A (``resolve_a2a_session`` → ``resolve_session`` → ``get_session``) and
artifact-by-ref (``registry.exists``) routes. Worse: for the web server,
"one owner thread" is not even a real invariant to begin with — FastAPI
runs a sync endpoint via its own threadpool, so no guard here can hold
structurally for that transport. The real fix belongs at the actual
accidental-safety window instead (a SEPARATE PR, not #5215): ``get_or_
load`` publishes a session to the registry's map (``_store_session``)
BEFORE its own later ``load_persisted_toggles``/``restore_state`` finish
constructing it — moving the publish to run LAST means any thread that
finds a session at all finds a complete one, and no read-side guard is
needed anywhere.

Docs-maintainer's B finding on the first version of this guard
(issuecomment-5384937050) still applies: a hand-counted list has no
completeness witness — stripping the guard from any ONE of the 4 mutators
must be independently caught, not just "some cross-thread test exists
somewhere". This file exercises EACH of the 4 mutators individually
(never one standing in for the others).

Acceptance (#4995 slice 1, architect issuecomment-5384963741):
  ① guard covers exactly the 4 mutators — not the 7 reads (see History
     above for why a second attempt at the reads, #5203/#5215, was
     withdrawn).
  ② the owner thread identity is a public read
     (:attr:`AgentRegistry.owner_thread_ident`), not a private-state reach-in

Real ``AgentRegistry`` + real ``threading.Thread`` (no mocks). The 4
``async def`` mutating methods are driven from the other thread via
``asyncio.run`` (a real, independent event loop on that thread) — the
identical shape ``ThreadedTransportProxy`` itself uses in production.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

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


def _call_from_other_thread(fn) -> "BaseException | None":
    """Run zero-arg callable *fn* on a REAL second OS thread and return
    whatever it raised (``None`` if it returned normally). ``fn`` may be
    sync or return a coroutine -- a coroutine is driven via ``asyncio.run``
    on that thread's own fresh loop (mirrors ``ThreadedTransportProxy``'s
    own worker-thread-owns-its-loop shape)."""
    caught: "list[BaseException | None]" = [None]

    def _run() -> None:
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        except BaseException as e:  # noqa: BLE001 -- captured for the main thread to inspect
            caught[0] = e

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "the other thread's call must have returned, not hung"
    return caught[0]


def test_owner_thread_ident_is_the_constructing_thread(tmp_path) -> None:
    """Tier 2: #4995 slice 1 acceptance ④ — the public read matches the
    thread that actually constructed the registry (this test's own thread)."""
    reg = _registry(tmp_path)
    assert reg.owner_thread_ident == threading.get_ident()


def test_same_thread_access_never_raises(tmp_path) -> None:
    """Tier 2: #4995 slice 1 acceptance ① — sanity/non-regression. Every
    one of the 4 mutating methods is reachable from the owner thread — the
    guard only ever fires against a DIFFERENT thread, never against the one
    that constructed the registry. The 7 reads are exercised too (never
    asserted, on any thread — see History in this file's own module
    docstring for why they stay unguarded), so a same-thread call staying
    silent is not itself informative for them; they are here for parity
    with the mutators' own coverage, not as a guard witness."""
    reg = _registry(tmp_path)
    # Reads (never asserted, on any thread — see module docstring):
    reg.exists("default")
    reg.loaded_names()
    reg.agent_workspace_dir("default")
    reg.get_session("default")
    reg.attached_session()
    reg.agent_cost_usd("default")
    reg.agent_total_usage("default")
    # Mutations (asserted, but this IS the owner thread):
    reg.record_background_attach_error("probe")
    asyncio.run(reg.attach("default", start_runner=False))
    asyncio.run(reg.restore_all())
    asyncio.run(reg.resume_deferred_agents())
    # No assertion needed beyond "did not raise" -- this test's only job is
    # to prove the guard changes nothing for the owner thread.


_MUTATING_METHOD_CALLS = {
    "attach": lambda reg: reg.attach("default", start_runner=False),
    "restore_all": lambda reg: reg.restore_all(),
    "resume_deferred_agents": lambda reg: reg.resume_deferred_agents(),
    "record_background_attach_error": lambda reg: reg.record_background_attach_error("x"),
}
# #4995 (architect: keep this hand-written, do NOT derive it from source).
# Deliberately the mirror of #5201's own lesson, not the same shape: #5201's
# hand-written list stood in for the REAL vocabulary (deriving it would have
# let the real thing grow past the list unnoticed). Here the hand-written
# list stands in for "the methods that are SUPPOSED to be guarded" — deriving
# it (e.g. from every method whose body assigns ``self._x``) would make BOTH
# sides of this test's assertion move together, so removing a guard AND its
# entry from a derivation source at once would stay green (six-questions ②,
# the identical-expression-on-both-sides failure).
#
# Disclosed gap, not a completeness claim: a FUTURE new mutating method
# added to ``AgentRegistry`` with no ``_assert_owner_thread()`` call is NOT
# caught by anything here or in the source — no zero-false-positive gate
# exists for "which methods mutate" (that is semantics, not syntax; a naive
# ``self._x =`` census over-fires on cached/memoized reads). The person
# adding that method must remember to add BOTH the guard call and an entry
# here — this test only proves the 4 CURRENTLY listed stay guarded.
#
# A guard-restoration attempt on the 7 reads was tried and WITHDRAWN
# (#5203/#5215, see this file's own module docstring's History section) —
# this dict stays MUTATORS-ONLY, not a placeholder for a sibling that will
# return: the reads are not merely "less important", the invariant itself
# does not hold for them given the web server's threadpool-per-sync-
# endpoint topology.


@pytest.mark.parametrize("method_name", sorted(_MUTATING_METHOD_CALLS))
def test_each_mutating_method_individually_raises_from_another_thread(
    tmp_path, method_name,
) -> None:
    """Tier 2: #4995 slice 1 acceptance ② — docs-maintainer's completeness
    finding on the FIRST version of this guard (issuecomment-5384937050):
    a single cross-thread test does not witness the other 3. Parametrized
    over all 4 mutating methods so stripping the guard from ANY ONE of
    them is independently caught (RED for that specific method, not
    masked by the other 3 still passing)."""
    reg = _registry(tmp_path / method_name)
    call = _MUTATING_METHOD_CALLS[method_name]
    caught = _call_from_other_thread(lambda: call(reg))
    assert isinstance(caught, RuntimeError), (
        f"{method_name}() reached from a different thread must raise "
        f"RuntimeError, got: {caught!r}"
    )
    assert "4995" in str(caught), "the error must point at the invariant's own issue"


def test_two_different_registries_each_owned_by_their_own_constructing_thread(
    tmp_path,
) -> None:
    """Tier 2: falsification contrast — ownership is per-INSTANCE, not a
    process-global "the first thread wins" latch. A second registry
    constructed on a DIFFERENT thread is owned by THAT thread."""
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
    # main_reg (owned by THIS thread) is safely mutable from it -- proof
    # the two instances' ownership is independent, not a shared/global latch.
    main_reg.record_background_attach_error("probe")
