# scaffold: triggered_by="#3082 Family 6b (history-compaction bundle builder) extraction lands"
# scaffold: removed_by="The same PR that lands the extraction, once this test is green"
"""Tier 1: byte-identical characterization gate for the #3082 Family 6b
extraction — ``Session._build_history_compaction_bundle`` pulling
``history_buffer`` (``RouterHistoryBuffer``) / ``compaction_controller``
(``CompactionController`` wrapping a ``CompactionEngine``) / ``budget_advisor``
(``ContextBudgetAdvisor``) — including the None-then-patch that breaks their
circular dependency — out of ``Session.__init__`` into one builder returning
one typed bundle (``_HistoryCompactionBundle``). Family 6a (``router_host``,
#3113) is NOT touched by this extraction — it is consumed here only as an
already-built cross-family dependency (``self._router_host``).

★ This family's crux, and this scaffold's primary target: ``history_buffer``
is constructed with ``compaction_controller=None`` first; ``compaction_
controller`` (whose inner ``CompactionEngine`` reads ``history_buffer.
build_system_prompt`` at CONSTRUCTION time, to compute budgets) is built
second; then a PATCH (``history_buffer._compaction_controller =
compaction_controller``) closes the cycle. ``self._history_buffer`` is only
assigned by ``__init__`` AFTER the builder RETURNS — so every one of these
intra-family references (``system_prompt_provider``, the patch line, and
``budget_advisor``'s ``compaction_controller=`` / ``history_fn=``) MUST read
the builder's LOCAL ``history_buffer`` / ``compaction_controller`` variables,
never ``self._history_buffer`` / ``self._compaction_controller`` — reading
``self._X`` from inside the builder would raise ``AttributeError`` (the
attribute does not exist yet at that point). This scaffold pins that
directly (``test_builder_does_not_crash_when_self_history_buffer_is_unset``)
by deleting ``self._history_buffer`` / ``self._compaction_controller`` /
``self._budget_advisor`` from an already-constructed Session (reproducing the
EXACT in-flight state the builder sees during ``__init__``, mirroring Family
5's "does not crash before chat_events exists" idiom) and calling the builder
directly — an accidental ``self._history_buffer`` read anywhere in the
None-then-patch chain would surface here as a real ``AttributeError``, not a
silent mis-wire.

Per the extracted-refactor idiom (docs/deep-dives/contributing/testing.md
Annex: Scaffolding tests / CLAUDE.md's byte-identical-staged-externalization
rule), this scaffold is added and removed in the SAME PR that lands the
extraction, once green.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. Private-attribute
reads are resolved to a LOCAL variable on a line BEFORE the ``assert`` and
are the extraction's OWN target attributes — ``session._history_buffer`` /
``session._compaction_controller`` are Session's own state (Family 4/5/6a's
accepted idiom), and ``history_buffer._compaction_controller`` is the EXACT
attribute this extraction's forward-patch sets (its own construction target,
not a faked collaborator's unrelated internals) — mirrored one level deeper
here because the forward-patch identity IS this family's crux, and is
explicitly named as the primary scaffold pin in the architect's Family 6b
spec (#3082 issue comment). Where a genuinely public, behavioral proof is
available (crash-avoidance, deferred model_fn re-resolution via
``raw_context_window()``), it is used instead / in addition.
"""
from __future__ import annotations

import pytest

from reyn.llm.model_budget import get_max_input_tokens
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.runtime.services.context_budget_advisor import ContextBudgetAdvisor
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer
from reyn.runtime.session import Session, _HistoryCompactionBundle


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.chdir(tmp_path)
    return Session(agent_name="family6b-history-compaction-test")


class TestFamily6bHistoryCompactionBundleByteIdentical:
    # ── builder contract ─────────────────────────────────────────────────

    def test_session_holds_the_three_real_types(self, session: Session) -> None:
        """Tier 1: the builder assigns real ``RouterHistoryBuffer`` /
        ``CompactionController`` / ``ContextBudgetAdvisor`` instances onto
        Session — the extraction's core contract."""
        history_buffer = session._history_buffer
        compaction_controller = session._compaction_controller
        budget_advisor = session._budget_advisor
        assert isinstance(history_buffer, RouterHistoryBuffer)
        assert isinstance(compaction_controller, CompactionController)
        assert isinstance(budget_advisor, ContextBudgetAdvisor)

    def test_builder_returns_a_history_compaction_bundle(
        self, session: Session,
    ) -> None:
        """Tier 1: calling the builder directly (bound method on Session)
        returns a ``_HistoryCompactionBundle`` wrapping the three real
        types — the builder's contract independent of ``__init__`` unpack
        wiring."""
        bundle = session._build_history_compaction_bundle(
            merge_action_usage=lambda candidates: None,
        )
        assert isinstance(bundle, _HistoryCompactionBundle)
        assert isinstance(bundle.history_buffer, RouterHistoryBuffer)
        assert isinstance(bundle.compaction_controller, CompactionController)
        assert isinstance(bundle.budget_advisor, ContextBudgetAdvisor)

    # ── ★ the crux: intra-6b references must be LOCAL, not self._X ───────

    def test_builder_does_not_crash_when_self_history_buffer_is_unset(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ crux — reproduces the EXACT in-flight ``__init__``
        state the builder runs under (``self._history_buffer`` /
        ``self._compaction_controller`` / ``self._budget_advisor`` not yet
        assigned) and calls the builder directly. If ANY intra-family
        reference inside the None-then-patch chain (``system_prompt_
        provider``, the patch line, or ``budget_advisor``'s
        ``compaction_controller=`` / ``history_fn=``) had been left as
        ``self._history_buffer`` / ``self._compaction_controller`` instead
        of the builder's LOCAL variables, this raises ``AttributeError`` —
        exactly the crash this extraction must avoid. Mirrors Family 5's
        "does not crash before chat_events exists" idiom for its own
        eager/deferred crux."""
        del session._history_buffer
        del session._compaction_controller
        del session._budget_advisor

        bundle = session._build_history_compaction_bundle(
            merge_action_usage=lambda candidates: None,
        )

        assert isinstance(bundle, _HistoryCompactionBundle)

    # ── ★ forward-patch: the None-then-patch cycle must close ────────────

    def test_forward_patch_wires_history_buffer_to_the_same_compaction_controller(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ primary pin (architect spec) — after the builder's
        None-then-patch sequence runs, ``history_buffer._compaction_
        controller`` IS the SAME ``compaction_controller`` instance the
        bundle returns (not a fresh one, not left ``None``). This is the
        exact attribute the forward-patch sets — the extraction's own
        construction target, not a faked collaborator's internals."""
        bundle = session._build_history_compaction_bundle(
            merge_action_usage=lambda candidates: None,
        )
        patched_controller = bundle.history_buffer._compaction_controller
        assert patched_controller is bundle.compaction_controller

    def test_compaction_engine_budgets_reflect_the_wired_system_prompt_provider(
        self, session: Session,
    ) -> None:
        """Tier 1: behavioral complement to the identity pin above —
        ``compaction_controller``'s inner ``CompactionEngine`` successfully
        computed real budgets DURING construction by calling ``history_
        buffer.build_system_prompt`` (the LOCAL bound method, not ``self.
        _history_buffer``'s — which would not exist yet). A real,
        positive ``effective_trigger`` proves the call round-tripped
        through the real router_host-backed system-prompt assembly rather
        than raising or silently degrading."""
        history_buffer = session._history_buffer
        compaction_controller = session._compaction_controller
        engine = compaction_controller._engine
        effective_trigger = engine.budgets.effective_trigger
        assert effective_trigger > 0
        # Sanity: the provider really is the LOCAL history_buffer's bound
        # method (same underlying instance), not some other buffer's.
        provider_owner = engine._system_prompt_provider.__self__
        assert provider_owner is history_buffer

    def test_budget_advisor_wired_to_the_same_compaction_controller_and_history_buffer(
        self, session: Session,
    ) -> None:
        """Tier 1: ``budget_advisor`` (UP-moved ahead of Family 8's
        ``InterAgentMessaging``) is wired to the SAME LOCAL
        ``compaction_controller`` / ``history_buffer`` the rest of this
        family holds — not fresh instances."""
        budget_advisor = session._budget_advisor
        compaction_controller = session._compaction_controller
        history_buffer = session._history_buffer
        wired_compaction_controller = budget_advisor._compaction_controller
        wired_history_fn = budget_advisor._history_fn
        assert wired_compaction_controller is compaction_controller
        history_fn_owner = wired_history_fn.__self__
        history_fn_method = wired_history_fn.__func__
        assert history_fn_owner is history_buffer
        assert history_fn_method is RouterHistoryBuffer.build_history

    def test_up_move_leaves_inter_agent_messaging_independent_and_intact(
        self, session: Session,
    ) -> None:
        """Tier 1: safety check for the ``budget_advisor`` UP-move —
        Family 8's ``InterAgentMessaging`` (unmoved, still constructed
        right after this builder returns) is still built successfully and
        does not depend on any of this family's three components."""
        from reyn.runtime.services.inter_agent_messaging import InterAgentMessaging
        inter_agent_messaging = session._inter_agent_messaging
        assert isinstance(inter_agent_messaging, InterAgentMessaging)

    # ── deferred lambdas: model_fn must re-resolve at CALL time ──────────

    def test_budget_advisor_model_fn_reresolves_a_model_override_at_call_time(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ ``model_fn=lambda: self._resolver.resolve(self.model)
        .model`` must stay DEFERRED — a ``/model`` override set AFTER
        construction must flow through ``budget_advisor.raw_context_
        window()`` (public method) immediately. Proven end-to-end against
        two DIFFERENT real models with different real context windows
        (``get_max_input_tokens``, independently computed here) rather than
        a hardcoded number — if the lambda had been eager-ized (frozen at
        builder-call time), both overrides would keep showing the
        construction-time model's window."""
        budget_advisor = session._budget_advisor

        session._model_override = "openai/gpt-4o-mini"
        resolved_1 = session._resolver.resolve(session.model).model
        expected_1 = get_max_input_tokens(resolved_1, events=session._chat_events)
        window_1 = budget_advisor.raw_context_window()["window"]
        assert window_1 == expected_1

        session._model_override = "openai/gpt-3.5-turbo"
        resolved_2 = session._resolver.resolve(session.model).model
        expected_2 = get_max_input_tokens(resolved_2, events=session._chat_events)
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

    # ── strip-falsify: the identity check itself must be live ────────────

    def test_strip_falsify_forward_patch_identity_check_is_live(
        self, session: Session,
    ) -> None:
        """Tier 1: strip-falsify — a FRESH, never-patched
        ``RouterHistoryBuffer`` (constructed with ``compaction_
        controller=None``, exactly like the pre-patch state) must NOT be
        equal to the real wired ``compaction_controller`` — proving the
        identity check above is genuinely reading the live patched wiring,
        not a check that would trivially pass regardless (e.g. because
        both sides are always ``None``)."""
        bundle = session._build_history_compaction_bundle(
            merge_action_usage=lambda candidates: None,
        )
        fresh_history_buffer = RouterHistoryBuffer(
            history_fn=session._active_branch_history,
            compaction=session._compaction,
            compaction_controller=None,
            model_fn=lambda: session._resolver.resolve(session.model).model,
            events=session._chat_events,
            media_store=session._media_store,
            router_host=session._router_host,
            action_retrieval=session._action_retrieval,
            non_interactive=session._non_interactive,
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
