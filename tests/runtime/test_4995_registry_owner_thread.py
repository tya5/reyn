"""Tier 2: #4995 slice 1 — ``AgentRegistry`` gains an explicit, enforced
single-owner-thread invariant, RESTORED to its original scope (all 11
methods — 4 mutators + 7 reads) by #5203, after #5202 had to narrow it to
just the 4 mutators.

## History (read this before touching any test below)

Architect ruling (issuecomment-5384869791), CORRECTED after CI caught the
first version (issuecomment-5384963741, PR #5202): the guard broke a
legitimate feature — ``app.py``'s #4983 design deliberately read
conversation history off the event loop via ``asyncio.to_thread``, a real
second OS thread reaching 7 read-only registry methods through
``RegistryReadModel``. #5202 scoped the guard down to just the 4 mutators
(``attach``, ``restore_all``, ``resume_deferred_agents``,
``record_background_attach_error``) to stop breaking that (27 CI
failures, all the identical ``RuntimeError``).

Architect's OWN follow-up ruling (issuecomment-5385152402), after
e2e-coder's #5203 measurement: the safety #5202 left in place for the 7
reads was ACCIDENTAL, not designed — a real partial-construction window
exists in ``AgentRegistry.get_or_load`` (between ``_store_session`` and
its own later ``load_persisted_toggles``/``restore_state``), and the ONLY
reason nothing broke was that the ONE field ``app.py``'s off-thread reads
actually consume (``Session.history``) happens to already be hydrated
before a session is ever stored — a fact about today's field shape, not a
guarantee. #5203 (same PR as this file's own changes) closes that by
CONSTRUCTION instead of leaving it accidental: ``app.py``'s 2
``asyncio.to_thread`` call sites now resolve the registry-touching half
ON THE LOOP first (:meth:`~reyn.interfaces.repl.read_model.
ChatReadModel.resolve_conversation_history_source`), handing the worker
thread an already-resolved PLAIN VALUE
(:meth:`~reyn.interfaces.repl.read_model.ChatReadModel.
conversation_history_from_source`) instead of letting it call back into
the registry. With that hoist in place, NOTHING legitimately reaches this
registry off its owner thread anymore — so the guard goes back onto all 7
reads, in this SAME PR (architect's own acceptance ①: "①が入らないなら
やらないでください" — this restoration IS the PR's own body, not an
optional extra).

Docs-maintainer's B finding on the first version of this guard
(issuecomment-5384937050) still applies, now to BOTH groups: a
hand-counted list has no completeness witness — stripping the guard from
any ONE of the 11 methods must be independently caught, not just "some
cross-thread test exists somewhere". This file exercises EACH of the 4
mutators AND EACH of the 7 reads individually (never one standing in for
the others).

Acceptance (#5203, architect issuecomment-5385152402):
  ① restore ``_assert_owner_thread()`` onto the 7 reads #5202 excluded, in
     the SAME PR as the ``app.py`` hoist (this file's own subject).
  ② with the guard restored, CI stays green (the 27-failure regression
     does NOT reappear) — a read reached from another thread now DOES
     raise, but ``app.py`` no longer legitimately reaches it that way.
  ③ strip: reverting the ``app.py`` hoist (simulating the pre-#5203
     call shape — a worker thread calling the combined
     ``conversation_history()`` directly) flips a read to RED — proving
     the hoist, not luck, is what keeps this green. This file's own
     ``test_each_read_method_individually_raises_from_another_thread``
     IS that flip: it fails on the pre-#5203 combined-call shape and
     passes once the guard covers all 7 (see this file's own git history
     around #5202 for the exact before/after).
  ④ the owner thread identity is a public read
     (:attr:`AgentRegistry.owner_thread_ident`), not a private-state reach-in

Real ``AgentRegistry`` + real ``threading.Thread`` (no mocks). The 3
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
    one of the 4 mutating methods AND every one of the 7 read methods is
    reachable from the owner thread — the guard only ever fires against a
    DIFFERENT thread, never against the one that constructed the registry."""
    reg = _registry(tmp_path)
    # Reads (asserted as of #5203, but this IS the owner thread):
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
# #5203: the guard's own scope grew from 4 to 11 (4 mutators + 7 reads,
# restored). This dict stays MUTATORS-ONLY, not because the 7 reads are less
# important, but because they are a semantically distinct group (reads, not
# writes) — see ``_READ_METHOD_CALLS`` below, its own sibling dict, same
# hand-written discipline, same disclosed gap.


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


_READ_METHOD_CALLS = {
    "exists": lambda reg: reg.exists("default"),
    "loaded_names": lambda reg: reg.loaded_names(),
    "agent_workspace_dir": lambda reg: reg.agent_workspace_dir("default"),
    "get_session": lambda reg: reg.get_session("default"),
    "attached_session": lambda reg: reg.attached_session(),
    "agent_cost_usd": lambda reg: reg.agent_cost_usd("default"),
    "agent_total_usage": lambda reg: reg.agent_total_usage("default"),
}
# #5203 — the SAME hand-written, not-derived-from-source discipline
# ``_MUTATING_METHOD_CALLS`` above uses, applied to the 7 reads the guard is
# restored onto in this PR. These are EXACTLY the 7 names #5202's own PR
# body/commit history excluded when it narrowed the guard from 11 methods
# down to 4 — see this file's own module docstring for the full history.
#
# Disclosed gap, not a completeness claim: a FUTURE new read method added
# to ``AgentRegistry`` with no ``_assert_owner_thread()`` call is NOT
# caught by anything here or in the source, same limitation
# ``_MUTATING_METHOD_CALLS``'s own comment names for mutators.


@pytest.mark.parametrize("method_name", sorted(_READ_METHOD_CALLS))
def test_each_read_method_individually_raises_from_another_thread(
    tmp_path, method_name,
) -> None:
    """Tier 2: #5203 acceptance ①②③ — the read-side counterpart of
    ``test_each_mutating_method_individually_raises_from_another_thread``,
    parametrized over all 7 reads so stripping the guard from ANY ONE is
    independently caught. This is also the concrete realization of
    acceptance③'s strip-test: with ``app.py``'s hoist landed (this same
    PR), nothing legitimately calls any of these 7 off the owner thread
    anymore, so a genuine cross-thread call — the exact shape ``app.py``
    used to make, pre-#5203 — correctly raises. Reverting ``app.py``'s own
    hoist (going back to calling the combined ``conversation_history()``
    from inside ``asyncio.to_thread``) makes a REAL run hit this exact
    RuntimeError in production — independently demonstrated by hand for
    this PR: reverting ``_handle_session_attached_event``'s call site to
    the pre-#5203 shape flips all 3 of ``test_4983_session_switch_off_
    thread.py``'s tests to this same ``RuntimeError``, not just this
    unit-level one (not itself checked in, since it duplicates the revert
    this parametrization already covers structurally)."""
    reg = _registry(tmp_path / method_name)
    call = _READ_METHOD_CALLS[method_name]
    caught = _call_from_other_thread(lambda: call(reg))
    assert isinstance(caught, RuntimeError), (
        f"{method_name}() reached from a different thread must raise "
        f"RuntimeError now that #5203 hoists the registry touch onto the "
        f"loop, got: {caught!r}"
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
