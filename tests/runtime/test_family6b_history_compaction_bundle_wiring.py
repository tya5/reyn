"""Tier 1/2: #3082 Family 6b (``Session._build_history_compaction_bundle``)
wiring — permanent home for 5 invariants rescued from
``tests/scaffold/test_family6b_history_compaction_bundle_byte_identical.py``
(part of #4862's family-by-family scaffold retirement, following #4868's
own precedent for family3: a rescue is either A — add/use a public read-hook
so an identity or effect is witnessed at the public surface, or B — keep
the private read where it demonstrably has no public alternative, per this
family's own accepted idiom below; never a silent compromise).

``Session._build_history_compaction_bundle`` wires ``RouterHistoryBuffer`` /
``CompactionController`` (wrapping a ``CompactionEngine``) / ``ContextBudget
Advisor`` together via a None-then-patch sequence that closes their circular
dependency (``history_buffer`` built first with ``compaction_controller=
None``; ``compaction_controller``'s engine reads ``history_buffer.build_
system_prompt`` at construction; then ``history_buffer._compaction_
controller`` is patched to the real controller). The scaffold's own
docstring establishes the accepted idiom this file inherits: private reads
resolved to a local variable are legitimate when they target the FAMILY'S
OWN construction state — ``session._history_buffer`` / ``session._
compaction_controller`` / ``session._budget_advisor`` (Session's own state,
Family 4/5/6a's precedent) and ``history_buffer._compaction_controller``
(the exact attribute the forward-patch sets — its own construction target,
not a faked collaborator's unrelated internals). Where a genuinely public
or behavioral proof exists, it is used instead — this file strengthens 1
of the 5 migrated invariants that way (④ below, with a dedicated
strip-falsify test proving the effect witness actually discriminates
broken wiring). ①②③ keep a private identity check: ①/② because no public
accessor exists on ``RouterHistoryBuffer``/``CompactionEngine`` for the
crux they pin, and ③ because an effect-based rescue WAS attempted and
then DISPROVEN by falsification — ``ContextBudgetAdvisor.context_window_
status()``'s ``effective_trigger`` turned out to be a pure function of
session-level state, so a poisoned/independent controller built from the
SAME session produces the identical number regardless of which instance
is actually wired (see ③'s own docstring for the falsification numbers).
All three are flagged in their own docstrings, not silently accepted.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

import pytest

from reyn.llm.model_budget import get_max_input_tokens
from reyn.runtime.services.inter_agent_messaging import InterAgentMessaging
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.chdir(tmp_path)
    return make_session(agent_name="family6b-history-compaction-test")


# ── ① forward-patch: the None-then-patch cycle must close ──────────────────


def test_forward_patch_wires_history_buffer_to_the_same_compaction_controller(
    session: Session,
) -> None:
    """Tier 1: after the builder's None-then-patch sequence runs,
    ``history_buffer._compaction_controller`` IS the SAME
    ``compaction_controller`` instance the bundle returns (not a fresh one,
    not left ``None``).

    Kept as a private-attribute identity check (unchanged in shape from
    the scaffold): ``RouterHistoryBuffer`` publishes no ``compaction_
    controller`` accessor, and none of its public methods
    (``build_history``/``build_system_prompt``/``decompose_history_for_
    retry``) surface an observable difference between "wired to THIS
    specific instance" and "wired to a different but functionally
    identical instance" — there is no available effect to witness this
    from the outside. This is the exact attribute the forward-patch sets
    (its own construction target, not a faked collaborator's internals),
    matching the family's own accepted idiom above."""
    bundle = session._build_history_compaction_bundle()
    patched_controller = bundle.history_buffer._compaction_controller
    assert patched_controller is bundle.compaction_controller


# ── ② the crux's other half: engine budgets prove the round-trip ───────────


def test_compaction_engine_budgets_reflect_the_wired_system_prompt_provider(
    session: Session,
) -> None:
    """Tier 1: ``compaction_controller``'s inner ``CompactionEngine``
    successfully computed real budgets DURING construction by calling
    ``history_buffer.build_system_prompt`` (the LOCAL bound method, not
    ``self._history_buffer``'s — which would not exist yet at that point
    in ``__init__``). A real, positive ``effective_trigger`` (read via
    ``CompactionEngine.budgets`` — a public ``@property``) proves the call
    round-tripped through the real router_host-backed system-prompt
    assembly rather than raising or silently degrading.

    ``engine._system_prompt_provider.__self__ is history_buffer`` (the
    ownership identity below) stays a private read: ``CompactionEngine``
    publishes no accessor for its system-prompt provider, and — like ①
    above — no public method surfaces a distinguishable effect for "the
    SAME history_buffer" vs. "an equivalent one"."""
    bundle = session._build_history_compaction_bundle()
    history_buffer = bundle.history_buffer
    compaction_controller = bundle.compaction_controller
    engine = compaction_controller._engine
    effective_trigger = engine.budgets.effective_trigger  # CompactionEngine.budgets is public
    assert effective_trigger > 0
    provider_owner = engine._system_prompt_provider.__self__
    assert provider_owner is history_buffer


# ── ③ budget_advisor wiring — identity kept private (effect rescue tried, ──
# ── and disproven under falsification; see below) ──────────────────────────


def test_budget_advisor_compaction_controller_is_the_wired_instance(
    session: Session,
) -> None:
    """Tier 1: ``budget_advisor._compaction_controller`` IS the SAME LOCAL
    ``compaction_controller`` instance the rest of this family holds — not
    a fresh one.

    Kept as a private-attribute identity check, NOT because a rescue
    wasn't attempted: an effect-based version (comparing ``budget_
    advisor.context_window_status()["effective_trigger"]`` against
    ``compaction_controller``'s own budgets across a real ``rebuild_
    engine()`` + ``/model``-override mutation, mirroring ⑤'s technique
    below) was written and then DISPROVEN by falsification, not merely
    passed and trusted. ``effective_trigger`` turned out to be a pure
    function of ``session``-level state (the live-resolved model, SP
    text, config) — every ``CompactionController`` built from the SAME
    session computes the identical number regardless of which specific
    instance ``budget_advisor`` actually holds, so an INDEPENDENT
    (uninvolved, never-rebuilt) controller from a second
    ``_build_history_compaction_bundle()`` call reflected the exact SAME
    post-mutation value (verified by hand: 9237 == 9237, both sides,
    with the real controller under test never even referenced by the
    poisoned advisor). A value-equality effect witness cannot discriminate
    "same instance" from "a different instance computing the same pure
    function of the same shared session" — there is no public surface
    this family exposes that depends on ``compaction_controller``'s
    OBJECT IDENTITY rather than its (session-determined) output, so this
    stays a direct identity check on the family's own construction
    target (this scaffold's originally accepted idiom)."""
    bundle = session._build_history_compaction_bundle()
    compaction_controller = bundle.compaction_controller
    budget_advisor = bundle.budget_advisor
    wired_compaction_controller = budget_advisor._compaction_controller
    assert wired_compaction_controller is compaction_controller


def test_budget_advisor_history_fn_is_the_wired_history_buffers_build_history(
    session: Session,
) -> None:
    """Tier 1: ``budget_advisor._history_fn`` is the LOCAL ``history_
    buffer``'s bound ``build_history`` method — not a fresh instance's.
    Kept as a private-attribute identity check: ``ContextBudgetAdvisor``
    publishes no accessor for its history function, matching ① / ②'s
    same "own construction target, no public route" reasoning."""
    bundle = session._build_history_compaction_bundle()
    history_buffer = bundle.history_buffer
    budget_advisor = bundle.budget_advisor
    wired_history_fn = budget_advisor._history_fn
    assert wired_history_fn.__self__ is history_buffer
    assert wired_history_fn.__func__ is RouterHistoryBuffer.build_history


# ── ④ up-move safety: InterAgentMessaging construction — rescued via effect


@pytest.mark.asyncio
async def test_up_move_leaves_inter_agent_messaging_independent_and_intact(
    session: Session,
) -> None:
    """Tier 2: rescue for the scaffold's bare ``isinstance(session._
    inter_agent_messaging, InterAgentMessaging)`` — which proved
    construction succeeded but nothing about "independent and intact"
    (the claim the test's own name makes). Strengthened two ways:

    1. ``InterAgentMessaging`` itself is exercised through a real public
       method (``handle_agent_response`` — the simplest complete round
       trip: resolves a pending chain and appends receiver-side history,
       no router re-invocation needed) against a chain registered via
       ``session.chains`` (Family 7, public since #4866/#4864) — proving
       Family 8a's construction genuinely depends on nothing Family 6b's
       up-move disturbs.
    2. Family 7 (``session.chains``) itself, built immediately BEFORE
       Family 8a in ``__init__``, is proven independently healthy
       (register + find), the actual "intact" claim the original test
       name made but never checked.
    """
    inter_agent_messaging = session._inter_agent_messaging
    assert isinstance(inter_agent_messaging, InterAgentMessaging)

    # TWO waiting delegates, only ONE replies here — the chain must stay
    # PARTIALLY resolved (waiting_on shrinks by exactly one, the pending
    # chain is neither dropped nor re-run through the router). A single
    # delegate would instead drive the chain to full resolution, which
    # re-invokes the router loop — too heavy for this wiring check and
    # not what this test is about.
    chain_id = "family6b-up-move-witness"
    await session.chains.register(
        chain_id=chain_id, depth=1, original_text="probe", sender="peer-agent-a",
        waiting_on={"peer-agent-a", "peer-agent-b"},
    )
    assert session.chains.has(chain_id)

    await inter_agent_messaging.handle_agent_response(
        {
            "chain_id": chain_id,
            "from_agent": "peer-agent-a",
            "response": "ack from a",
        }
    )
    resolved = session.chains.get(chain_id)
    assert resolved is not None and resolved.waiting_on == {"peer-agent-b"}, (
        "handle_agent_response must drop peer-agent-a from waiting_on on "
        "the SAME ChainManager session.chains exposes — if "
        "InterAgentMessaging held an independent/broken chains reference, "
        "waiting_on would be unchanged"
    )


@pytest.mark.asyncio
async def test_strip_falsify_inter_agent_messaging_chains_wiring_is_live(
    session: Session,
) -> None:
    """Tier 2: strip-falsify for the test above — reproduces the EXACT
    broken-wiring scenario it claims to catch, by hand-poisoning
    ``InterAgentMessaging._chains`` to an INDEPENDENT ``ChainManager``
    (never ``session.chains``) before calling ``handle_agent_response``.
    Verified by direct experimentation before this test was written
    (poisoned run: ``waiting_on`` stayed ``{peer-agent-a, peer-agent-b}``
    — unchanged — while the positive test's own real wiring correctly
    shrinks it). If this test ever went green with an UNCHANGED
    ``waiting_on``, the positive test above would be proven vacuous."""
    from reyn.core.events.events import EventLog
    from reyn.runtime.services.chain_manager import ChainManager

    inter_agent_messaging = session._inter_agent_messaging
    chain_id = "family6b-strip-falsify"
    await session.chains.register(
        chain_id=chain_id, depth=1, original_text="probe", sender="peer-agent-a",
        waiting_on={"peer-agent-a", "peer-agent-b"},
    )

    poisoned_chains = ChainManager(
        journal=None, events=EventLog(), chain_timeout_seconds=0, max_hop_depth=10,
    )
    inter_agent_messaging._chains = poisoned_chains  # deliberate poison, this test only

    await inter_agent_messaging.handle_agent_response(
        {"chain_id": chain_id, "from_agent": "peer-agent-a", "response": "ack from a"}
    )

    resolved = session.chains.get(chain_id)
    assert resolved is not None and resolved.waiting_on == {
        "peer-agent-a", "peer-agent-b",
    }, (
        "with InterAgentMessaging poisoned to an independent ChainManager, "
        "session.chains's own pending entry must be UNCHANGED — proving "
        "the positive test's assertion is genuinely reading live wiring, "
        "not something that would pass regardless"
    )


# ── ⑤ deferred lambdas: model_fn must re-resolve at CALL time ──────────────


def test_budget_advisor_model_fn_reresolves_a_model_override_at_call_time(
    session: Session,
) -> None:
    """Tier 1: ``model_fn=lambda: self._resolver.resolve(self.model)
    .model`` must stay DEFERRED — a ``/model`` override set AFTER
    construction must flow through ``budget_advisor.raw_context_
    window()`` (public method) immediately. Proven end-to-end against two
    DIFFERENT real models with different real context windows
    (``get_max_input_tokens``, independently computed here) rather than a
    hardcoded number — if the lambda had been eager-ized (frozen at
    builder-call time), both overrides would keep showing the
    construction-time model's window.

    ``session._model_override`` is set directly, matching production's
    own idiom — the ``/model`` slash command handler
    (``interfaces/slash/model.py``) and ``session_api.py``'s AgentStep
    ``model=`` override both set this SAME attribute directly; there is
    no public setter anywhere in the codebase to defer to.

    ``ensure_litellm_ready()`` is called first (a real, blocking
    synchronization on the SAME chokepoint ``get_max_input_tokens``
    itself uses to import litellm) — not a race. Without it,
    ``get_max_input_tokens`` silently falls back to a conservative
    128,000-token placeholder while litellm's background warm-up thread
    is still loading, which can make BOTH probe models report the exact
    same fallback number and turn this test vacuous (observed directly:
    reproduced 128000 == 128000 when this file was run standalone,
    before this call was added)."""
    from reyn.llm.litellm_bootstrap import ensure_litellm_ready
    ensure_litellm_ready()

    budget_advisor = session._budget_advisor

    session._model_override = "openai/gpt-4o-mini"
    resolved_1 = session._resolver.resolve(session.model).model
    expected_1 = get_max_input_tokens(resolved_1, events=session._audit_events)
    window_1 = budget_advisor.raw_context_window()["window"]
    assert window_1 == expected_1

    session._model_override = "openai/gpt-3.5-turbo"
    resolved_2 = session._resolver.resolve(session.model).model
    expected_2 = get_max_input_tokens(resolved_2, events=session._audit_events)
    window_2 = budget_advisor.raw_context_window()["window"]
    assert window_2 == expected_2
    assert expected_1 != expected_2, (
        "the two probe models must have different real context windows "
        "for this check to be non-vacuous"
    )
    assert window_1 != window_2, (
        "strip-falsify: model_fn did not re-resolve self._model_override "
        "after reassignment — eager-ized, not deferred"
    )


# ── strip-falsify: ①'s identity check must be genuinely live ───────────────


def test_strip_falsify_forward_patch_identity_check_is_live(session: Session) -> None:
    """Tier 1: strip-falsify for ① — a FRESH, never-patched
    ``RouterHistoryBuffer`` (constructed with ``compaction_controller=
    None``, exactly like the pre-patch state) must NOT be equal to the
    real wired ``compaction_controller``, proving ①'s identity check is
    genuinely reading the live patched wiring, not a check that would
    trivially pass regardless (e.g. because both sides are always
    ``None``)."""
    bundle = session._build_history_compaction_bundle()
    fresh_history_buffer = RouterHistoryBuffer(
        history_fn=session._active_branch_history,
        compaction=session._compaction,
        compaction_controller=None,
        model_fn=lambda: session._resolver.resolve(session.model).model,
        events=session._audit_events,
        media_store=session._media_store,
        router_host=session.router_host,
        universal_wrappers_enabled=session._universal_wrappers_enabled,
        non_interactive=session.non_interactive,
        reasoning=session._reasoning,
    )
    fresh_patched_controller = fresh_history_buffer._compaction_controller
    real_patched_controller = bundle.history_buffer._compaction_controller
    real_compaction_controller = bundle.compaction_controller
    assert fresh_patched_controller is None
    assert real_patched_controller is not None
    assert real_patched_controller is real_compaction_controller


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
