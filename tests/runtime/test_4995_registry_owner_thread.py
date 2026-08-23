"""Tier 2: #4995 slice 1 — ``AgentRegistry`` gains an explicit, enforced
single-owner-THREAD-FOR-MUTATION invariant.

Architect ruling (issuecomment-5384869791), CORRECTED after CI caught the
first version (issuecomment-5384963741, PR #5202): the invariant is "only
one thread may MUTATE this registry", not "only one thread may touch it
at all" — ``app.py``'s #4983 design deliberately reads conversation
history off the event loop via ``asyncio.to_thread``, a real second OS
thread reaching several read-only registry methods through
``RegistryReadModel``. Asserting on those 7 reads broke that legitimate
feature (27 CI failures, all the identical ``RuntimeError``). The guard
now covers exactly the 4 methods that MUTATE registry-owned state:
``attach``, ``restore_all``, ``resume_deferred_agents``,
``record_background_attach_error``.

Docs-maintainer's B finding on the first version (issuecomment-5384937050,
still valid against the corrected scope): a hand-counted list has no
completeness witness — stripping the guard from any ONE of the 4 methods
must be independently caught, not just "some cross-thread test exists
somewhere". This file exercises EACH of the 4 individually (never one
standing in for the others), plus a read-side falsification (a read
reached from a second thread must NOT raise — the #4983 pattern this
scope correction exists to preserve).

Acceptance:
  ① same-thread access to every one of the 4 + every one of the 7 reads
     never raises (sanity — slice 1's own non-regression)
  ② EACH of the 4 mutating methods raises RuntimeError when reached from
     a genuinely different OS thread, tested individually
  ③ a read method reached from a different OS thread does NOT raise (the
     #4983 pattern preserved — falsification contrast for ②)
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
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
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
    reachable from the owner thread exactly as before slice 1."""
    reg = _registry(tmp_path)
    # Reads (never asserted, on any thread):
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
    # to prove slice 1 changed nothing for the owner thread.


_MUTATING_METHOD_CALLS = {
    "attach": lambda reg: reg.attach("default", start_runner=False),
    "restore_all": lambda reg: reg.restore_all(),
    "resume_deferred_agents": lambda reg: reg.resume_deferred_agents(),
    "record_background_attach_error": lambda reg: reg.record_background_attach_error("x"),
}


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


def test_a_read_reached_from_another_thread_does_not_raise(tmp_path) -> None:
    """Tier 2: #4995 slice 1 acceptance ③ — falsification contrast for ②,
    and the exact regression the first version of this guard caused (CI,
    27 failures, PR #5202): ``app.py``'s #4983 design reads conversation
    history off the event loop via ``asyncio.to_thread`` -- a REAL second
    OS thread reaching ``get_session`` (and siblings) through
    ``RegistryReadModel``. That must keep working. RED if a read method
    is ever given the mutation-only guard back."""
    reg = _registry(tmp_path)
    caught = _call_from_other_thread(lambda: reg.get_session("default", _DEFAULT_SID))
    assert caught is None, (
        f"a read reached from a different thread must NOT raise (the #4983 "
        f"off-thread-read pattern this scope exists to preserve), got: {caught!r}"
    )


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
