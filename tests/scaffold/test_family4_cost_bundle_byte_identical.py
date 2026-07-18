# scaffold: triggered_by="#3082 Family 4 (cost/budget bundle builder) extraction lands"
# scaffold: removed_by="The same PR that lands the extraction, once this test is green"
"""Tier 1: byte-identical characterization gate for the #3082 Family 4
extraction — ``Session._build_cost_bundle`` pulling the ``BudgetGateway``
construction out of ``Session.__init__`` into one builder returning one
typed bundle (``_CostBundle``). The simplest of the #3082 families: a single
unconditional component, no intra-family DAG, no reordering (the builder is
invoked at its original inline call site, unmoved).

This is a pure output->input builder pipeline stage: there is no separate
"old" implementation left in the tree to diff against (the inline sequence
was replaced, not duplicated), so byte-identical is pinned as the set of
*wiring invariants* the inline sequence guaranteed and the builder must
reproduce exactly — pinned BEHAVIORALLY (drive the wired object, observe the
downstream effect through its PUBLIC surface) rather than by peeking at
private identity (the Family 2/3 lesson):

1. ``budget.tracker IS`` the LOCAL ``budget_tracker`` __init__ parameter
   (never ``self._budget_tracker``, a separate later tracking assignment out
   of scope for this extraction) — observed via ``BudgetGateway.tracker``'s
   own public property, not a private ``_tracker`` peek.
2. ``budget``'s ``events`` sink IS Family 1's ``chat_events`` (the
   value-dependency this family shares with Family 3's ``hot_reloader`` —
   ``BudgetGateway`` reads ``events`` EAGERLY at construction, which is why
   the family is built after Family 1) — proven end-to-end: exhausting the
   router cap emits a ``router_retry_exhausted`` P6 event that lands in
   ``session._chat_events``, observed via the EventLog's public ``all()``
   read.
3. ``router_cap`` passes through into the gateway's public ``router_cap``
   property.

Strip-falsify (invariant 2 is live / non-vacuous): re-running the
router-cap-exhaustion probe against a bundle built with a DIFFERENT
(fresh) EventLog instead of ``session._chat_events`` shows the
``router_retry_exhausted`` event does NOT land in ``session._chat_events`` —
proving the invariant-2 observation genuinely depends on the gateway being
wired to ``session._chat_events``, not a check that would pass regardless.

No new WAL-derived recovery state is introduced by this extraction (pure
move of existing construction code), so the CLAUDE.md truncate-falsify
recovery-feature PR gate does not apply. Per the extracted-refactor idiom
(``docs/deep-dives/contributing/testing.md`` Annex: Scaffolding tests /
CLAUDE.md's byte-identical-staged-externalization rule), this scaffold is
added and removed in the SAME PR that lands the extraction, once green.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

import pytest

from reyn.core.events.events import EventLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.errors import RouterCapExceeded
from reyn.runtime.services.budget_gateway import BudgetGateway
from reyn.runtime.session import Session, _CostBundle


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.chdir(tmp_path)
    return Session(agent_name="family4-cost-bundle-test")


def _exhaust_router_cap(budget: BudgetGateway) -> None:
    """Drive ``budget`` past its configured ``router_cap`` (read via the
    gateway's own public property, so this helper works whether the cap is 1,
    3 [the default], or anything else) so the call ONE PAST the cap raises
    ``RouterCapExceeded`` and, on the way, emits ``router_retry_exhausted``
    onto the gateway's wired ``events`` sink."""
    for _ in range(budget.router_cap):
        budget.check_and_increment_router_cap("call")  # increments, no raise
    with pytest.raises(RouterCapExceeded):
        budget.check_and_increment_router_cap("one past cap")  # cap reached -> raises


class TestFamily4CostBundleByteIdentical:
    def test_session_holds_budget_attr_of_real_type(self, session: Session) -> None:
        """Tier 1: the builder assigns the same real object type
        (``BudgetGateway``) the inline sequence built, on the same
        ``Session._budget`` attribute."""
        is_budget_gateway = isinstance(session._budget, BudgetGateway)
        assert is_budget_gateway

    def test_builder_returns_a_cost_bundle(self, session: Session) -> None:
        """Tier 1: calling the builder directly (public method on Session)
        returns a ``_CostBundle`` wrapping a real ``BudgetGateway`` — the
        builder's contract independent of ``__init__`` unpack wiring."""
        bundle = session._build_cost_bundle(
            None,                  # budget_tracker
            session._chat_events,  # chat_events (Family 1 EventLog)
            session.agent_name,
            3,                     # router_cap
        )
        assert isinstance(bundle, _CostBundle)
        assert isinstance(bundle.budget, BudgetGateway)

    def test_budget_tracker_local_param_is_wired(self, tmp_path, monkeypatch) -> None:
        """Tier 1: invariant 1 — the builder wires the LOCAL ``budget_tracker``
        __init__ parameter into the gateway, observed via ``BudgetGateway
        .tracker``'s own public property (not a private ``_tracker`` peek)."""
        monkeypatch.chdir(tmp_path)
        tracker = BudgetTracker(CostConfig())
        s = Session(agent_name="family4-tracker-wiring-test", budget_tracker=tracker)
        wired_tracker = s._budget.tracker
        assert wired_tracker is tracker

    def test_router_cap_param_passthrough(self, session: Session) -> None:
        """Tier 1: invariant 3 — ``router_cap`` passes through into the
        gateway's public ``router_cap`` property."""
        bundle = session._build_cost_bundle(
            None, session._chat_events, session.agent_name, 7,
        )
        assert bundle.budget.router_cap == 7

    def test_budget_events_is_the_family_chat_events(self, session: Session) -> None:
        """Tier 1: invariant 2 — the REAL, ``__init__``-wired
        ``session._budget``'s ``events`` sink IS Family 1's ``chat_events``
        (drives the actual call-site wiring, not a manually reconstructed
        bundle). Exhausting the router cap emits a ``router_retry_exhausted``
        P6 event that lands in ``session._chat_events`` (EventLog ``all()``)
        iff the sink IS that EventLog."""
        before = len(session._chat_events.all())
        _exhaust_router_cap(session._budget)
        after = [e.type for e in session._chat_events.all()]
        assert len(after) > before
        assert "router_retry_exhausted" in after

    def test_strip_falsify_budget_events_wiring_is_live(self, session: Session) -> None:
        """Tier 1: strip-falsify — re-run invariant 2's probe against a
        gateway built with a DIFFERENT (fresh) EventLog instead of
        ``session._chat_events`` (same construction the builder performs,
        just pointed at an unrelated log). The ``router_retry_exhausted``
        event must NOT land in ``session._chat_events`` — proving the
        invariant-2 observation genuinely depends on the REAL
        ``session._budget`` being wired to ``session._chat_events``, not a
        check that would pass regardless of wiring (non-vacuous)."""
        other_events = EventLog()
        poisoned = BudgetGateway(
            budget_tracker=None, events=other_events,
            agent_name=session.agent_name, default_router_cap=1,
        )
        before = len(session._chat_events.all())
        _exhaust_router_cap(poisoned)
        after = [e.type for e in session._chat_events.all()]
        assert len(after) == before, (
            "strip-falsify: a router-cap-exhaustion event reached "
            "session._chat_events through a gateway wired to an UNRELATED "
            "EventLog — the invariant-2 check would be vacuous"
        )
        # confirm the event actually fired (just onto the OTHER log)
        assert "router_retry_exhausted" in [e.type for e in other_events.all()]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
