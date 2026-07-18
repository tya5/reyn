# scaffold: triggered_by="#3082 Family 8a (inter_agent_messaging bundle builder) extraction lands"
# scaffold: removed_by="The same PR that lands the extraction, once this test is green"
"""Tier 1: byte-identical characterization gate for the #3082 Family 8a
extraction — ``Session._build_inter_agent_messaging_bundle`` pulling
``inter_agent_messaging`` (``InterAgentMessaging``) out of
``Session.__init__`` into one builder returning one typed bundle
(``_InterAgentMessagingBundle``).

★ Family 8 DAG correction (owned by the architect): the originally-planned
"Family 8 = memory → inter_agent_messaging + mcp_connection_service"
grouping does not hold — the five residual leftover components
(mcp_connection_service, render_template_bounds, task_subscription_writer,
memory, inter_agent_messaging) are mutually independent and straddle the
router-host WAIST (Family 6a) on both sides, so they cannot be gathered
into one builder. render_template_bounds and task_subscription_writer stay
inline (trivial — a 2-arg config and a 1-line ternary, not worth a
builder). The three substantial leaves — inter_agent_messaging (8a, this
PR), memory (8b), mcp_connection_service (8c) — each get their own no-move,
single-component builder, in separate PRs.

★ Post-waist, cross-family EAGER deps: ``inter_agent_messaging`` reads
Family 7's ``chains`` (``chain_manager=self._chains``) and Family 1's
``chat_events`` (``event_log=self._chat_events``) EAGERLY at construction
time — both already set on ``self`` by the time this builder runs (its
call site sits at the construction's ORIGINAL, unmoved position, right
after Family 7's ``_build_intervention_bundle`` returns).

★ Deferred lambda/bound-method tail (kept verbatim, never eager-ized):
``run_router_loop`` / ``get_router_loop_delegations`` /
``set_router_loop_delegations`` / ``get_router_loop_agent_replies`` /
``set_router_loop_agent_replies`` / ``session_id_fn`` all close over
``self`` and resolve per-turn / post-construction state at CALL time, long
after ``__init__`` returns. This scaffold pins that a value mutated on
``Session`` AFTER construction is visible through these closures (proving
they are not frozen snapshots taken at builder-call time).

Single independent leaf component (unlike Family 6b/7's multi-component
families) — no intra-family local-vs-self split applies, since there is
nothing else in this family to be local against.

Per the extracted-refactor idiom (docs/deep-dives/contributing/testing.md
Annex: Scaffolding tests / CLAUDE.md's byte-identical-staged-externalization
rule), this scaffold is added and removed in the SAME PR that lands the
extraction, once green.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. Private-attribute
reads are resolved to a LOCAL variable on a line BEFORE the ``assert`` and
are the extraction's OWN target attributes (``inter_agent_messaging._chains``
/ ``inter_agent_messaging._events``) — Session's own state plus the exact
wiring targets this extraction's eager-vs-deferred split is about (Family
4/5/6a/6b/7's accepted idiom).
"""
from __future__ import annotations

import pytest

from reyn.runtime.services.chain_manager import ChainManager
from reyn.runtime.services.inter_agent_messaging import InterAgentMessaging
from reyn.runtime.session import Session, _InterAgentMessagingBundle


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.chdir(tmp_path)
    return Session(agent_name="family8a-inter-agent-messaging-test")


class TestFamily8aInterAgentMessagingBundleByteIdentical:
    # ── builder contract ─────────────────────────────────────────────────

    def test_session_holds_the_real_type(self, session: Session) -> None:
        """Tier 1: the builder assigns a real ``InterAgentMessaging``
        instance onto Session — the extraction's core contract."""
        inter_agent_messaging = session._inter_agent_messaging
        assert isinstance(inter_agent_messaging, InterAgentMessaging)

    def test_builder_returns_an_inter_agent_messaging_bundle(
        self, session: Session,
    ) -> None:
        """Tier 1: calling the builder directly (bound method on Session)
        returns an ``_InterAgentMessagingBundle`` wrapping the real type —
        the builder's contract independent of ``__init__`` unpack wiring."""
        bundle = session._build_inter_agent_messaging_bundle()
        assert isinstance(bundle, _InterAgentMessagingBundle)
        assert isinstance(bundle.inter_agent_messaging, InterAgentMessaging)

    # ── ★ post-waist cross-family EAGER deps: chains (F7) / chat_events (F1) ──

    def test_chain_manager_is_the_same_chains_instance(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ F7→F8a cross-dep pin — ``inter_agent_messaging`` reads
        ``chain_manager=self._chains`` EAGERLY at construction time. This
        proves it is the SAME ``ChainManager`` instance Family 7 built, not
        a fresh one and not unset."""
        inter_agent_messaging = session._inter_agent_messaging
        wired_chains = inter_agent_messaging._chains
        session_chains = session._chains
        assert wired_chains is session_chains

    def test_event_log_is_the_same_chat_events_instance(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ F1→F8a cross-dep pin — ``inter_agent_messaging`` reads
        ``event_log=self._chat_events`` EAGERLY at construction time. This
        proves it is the SAME ``EventLog`` instance Family 1 built."""
        inter_agent_messaging = session._inter_agent_messaging
        wired_events = inter_agent_messaging._events
        session_events = session._chat_events
        assert wired_events is session_events

    # ── ★ deferred lambdas: resolve current value at CALL time, not builder-call time ──

    def test_get_router_loop_delegations_reflects_a_post_construction_mutation(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ deferred pin — ``get_router_loop_delegations`` is a
        ``lambda: self._router_loop_delegations`` closure. Mutating
        ``session._router_loop_delegations`` AFTER construction and AFTER
        the builder already returned must still be visible through the
        closure — proving it resolves live state at CALL time, not a
        snapshot frozen when the builder ran (which would have captured the
        pre-``__init__``-completion value)."""
        inter_agent_messaging = session._inter_agent_messaging
        get_delegations = inter_agent_messaging._get_router_loop_delegations

        assert get_delegations() is None

        sentinel = [{"probe": "post-construction-delegation"}]
        session._router_loop_delegations = sentinel

        assert get_delegations() is sentinel

    def test_set_router_loop_delegations_writes_back_onto_session(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ deferred pin — ``set_router_loop_delegations`` is a
        ``lambda v: setattr(self, "_router_loop_delegations", v)`` closure.
        Calling it through ``inter_agent_messaging`` must mutate
        ``session._router_loop_delegations`` itself (not a local copy),
        proving the setter closure is still bound to the live ``Session``,
        not a value captured at builder-call time."""
        inter_agent_messaging = session._inter_agent_messaging
        set_delegations = inter_agent_messaging._set_router_loop_delegations

        sentinel = [{"probe": "set-via-closure"}]
        set_delegations(sentinel)

        written_delegations = session._router_loop_delegations
        assert written_delegations is sentinel

    def test_session_id_fn_reflects_a_post_construction_mutation(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ deferred pin — ``session_id_fn`` is a
        ``lambda: self._session_id`` closure (#2103 S1bc-exec: spawned
        sessions stamp their LIVE sid post-construction, so a cached value
        would be stale). Mutating ``session._session_id`` AFTER
        construction must be visible through the closure."""
        inter_agent_messaging = session._inter_agent_messaging
        session_id_fn = inter_agent_messaging._session_id_fn

        session._session_id = "probe-post-construction-sid"

        assert session_id_fn() == "probe-post-construction-sid"

    # ── strip-falsify: the identity checks themselves must be live ───────

    def test_strip_falsify_chain_manager_check_is_live(
        self, session: Session,
    ) -> None:
        """Tier 1: strip-falsify for the F7→F8a cross-dep pin — a FRESH,
        independently-constructed ``ChainManager`` must NOT be the same
        instance ``inter_agent_messaging`` actually holds, proving that pin
        genuinely reads the live cross-family wiring rather than trivially
        passing regardless of what is wired."""
        fresh_chains = ChainManager(
            journal=session._journal,
            events=session._chat_events,
            chain_timeout_seconds=session._chain_timeout_seconds,
            max_hop_depth=session._max_hop_depth,
        )

        wired_chains = session._inter_agent_messaging._chains
        session_chains = session._chains

        assert fresh_chains is not session_chains
        assert wired_chains is session_chains
        assert wired_chains is not fresh_chains

    def test_strip_falsify_deferred_delegations_check_is_live(
        self, session: Session,
    ) -> None:
        """Tier 1: strip-falsify for the deferred-lambda pin — a value NOT
        written onto ``session._router_loop_delegations`` must NOT be what
        the getter closure returns, proving the getter pin is genuinely
        reading live Session state at call time rather than a value that
        would appear regardless of what is set."""
        get_delegations = session._inter_agent_messaging._get_router_loop_delegations
        never_written_sentinel = [{"probe": "never-written"}]

        assert get_delegations() is not never_written_sentinel

        session._router_loop_delegations = never_written_sentinel

        assert get_delegations() is never_written_sentinel


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
