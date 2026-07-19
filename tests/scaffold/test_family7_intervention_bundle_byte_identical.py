# scaffold: triggered_by="#3082 Family 7 (intervention bundle builder) extraction lands"
# scaffold: removed_by="The same PR that lands the extraction, once this test is green"
"""Tier 1: byte-identical characterization gate for the #3082 Family 7
extraction — ``Session._build_intervention_bundle`` pulling ``chains``
(``ChainManager``) / ``interventions`` (``InterventionRegistry``) /
``intervention_handler`` (``InterventionHandler``) / ``intervention_
coordinator`` (``InterventionCoordinator``) / ``chain_timeout_glue``
(``ChainTimeoutGlue``) out of ``Session.__init__`` into one builder
returning one typed bundle (``_InterventionBundle``). Five components — the
DAG grouping is accurate here (unlike Families 4/5, which needed a mid-arc
correction).

★ NO forward-patch / circular dependency (simpler than Family 6b's
history_buffer ↔ compaction_controller cycle): ``chains`` and ``chain_
timeout_glue`` reference each other, but ASYMMETRICALLY — ``chain_timeout_
glue`` reads ``chains`` EAGERLY (``chains=self._chains``, at construction
time), while ``chains`` only reaches ``chain_timeout_glue`` INDIRECTLY,
through the bound method ``Session._on_chain_timeout_fire`` (wired into
Family 8's ``InterAgentMessaging``, unmoved), which forwards to
``self._chain_timeout_glue.on_chain_timeout_fire`` only when CALLED — long
after both exist. So construction is strictly LINEAR: chains →
interventions → intervention_handler → intervention_coordinator →
chain_timeout_glue. No None-then-patch needed, unlike Family 6b.

★ ``chain_timeout_glue`` Family-8-straddling UP-move: originally
constructed AFTER Family 8's ``InterAgentMessaging``; this builder
constructs it as the last of the five components, landing the whole family
as one contiguous builder call BEFORE ``InterAgentMessaging`` (unmoved,
still constructed directly in ``__init__`` right after this builder
returns).

★★ Family-8 cross-dep: ``InterAgentMessaging`` reads ``chain_manager=self.
_chains`` — the builder call site sits at ``chains``'s ORIGINAL position,
so ``self._chains`` is assigned by ``__init__`` well before
``InterAgentMessaging`` is constructed. This scaffold pins that the F8→F7
dependency is preserved (``InterAgentMessaging``'s ``_chains`` IS the same
``ChainManager`` instance).

★ intra-Family-7 local-vs-self: ``self._interventions`` / ``self._
intervention_handler`` / ``self._chains`` are all assigned by ``__init__``
only AFTER this builder RETURNS — reading them as ``self._X`` from INSIDE
the builder would raise ``AttributeError``. This scaffold pins that
directly (``test_builder_does_not_crash_when_self_chains_is_unset``) by
deleting ``self._chains`` / ``self._interventions`` / ``self._intervention_
handler`` / ``self._intervention_coordinator`` / ``self._chain_timeout_
glue`` from an already-constructed Session (reproducing the EXACT in-flight
state the builder sees during ``__init__``, mirroring Family 5/6b's "does
not crash before X exists" idiom) and calling the builder directly.

Per the extracted-refactor idiom (docs/deep-dives/contributing/testing.md
Annex: Scaffolding tests / CLAUDE.md's byte-identical-staged-externalization
rule), this scaffold is added and removed in the SAME PR that lands the
extraction, once green.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. Private-attribute
reads are resolved to a LOCAL variable on a line BEFORE the ``assert`` and
are the extraction's OWN target attributes (``session._chains`` /
``coordinator._registry`` / ``coordinator._handler`` / ``handler._registry``
/ ``glue._chains`` / ``inter_agent_messaging._chains``) — Session's own
state plus the exact wiring targets this extraction's local-vs-self split
is about (Family 4/5/6a/6b's accepted idiom, mirrored one level deeper here
because the wiring identity IS this family's crux and is explicitly named
as the primary scaffold pin in the architect's Family 7 spec (#3082 issue
comment)).
"""
from __future__ import annotations

import pytest

from reyn.runtime.services.chain_manager import ChainManager
from reyn.runtime.services.chain_timeout_glue import ChainTimeoutGlue
from reyn.runtime.services.intervention_coordinator import InterventionCoordinator
from reyn.runtime.services.intervention_handler import InterventionHandler
from reyn.runtime.services.intervention_registry import InterventionRegistry
from reyn.runtime.session import Session, _InterventionBundle
from tests._support.agent_session import make_session


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.chdir(tmp_path)
    return make_session(agent_name="family7-intervention-test")


class TestFamily7InterventionBundleByteIdentical:
    # ── builder contract ─────────────────────────────────────────────────

    def test_session_holds_the_five_real_types(self, session: Session) -> None:
        """Tier 1: the builder assigns real ``ChainManager`` /
        ``InterventionRegistry`` / ``InterventionHandler`` /
        ``InterventionCoordinator`` / ``ChainTimeoutGlue`` instances onto
        Session — the extraction's core contract."""
        chains = session._chains
        interventions = session._interventions
        intervention_handler = session._intervention_handler
        intervention_coordinator = session._intervention_coordinator
        chain_timeout_glue = session._chain_timeout_glue
        assert isinstance(chains, ChainManager)
        assert isinstance(interventions, InterventionRegistry)
        assert isinstance(intervention_handler, InterventionHandler)
        assert isinstance(intervention_coordinator, InterventionCoordinator)
        assert isinstance(chain_timeout_glue, ChainTimeoutGlue)

    def test_builder_returns_an_intervention_bundle(self, session: Session) -> None:
        """Tier 1: calling the builder directly (bound method on Session)
        returns an ``_InterventionBundle`` wrapping the five real types —
        the builder's contract independent of ``__init__`` unpack wiring."""
        bundle = session._build_intervention_bundle()
        assert isinstance(bundle, _InterventionBundle)
        assert isinstance(bundle.chains, ChainManager)
        assert isinstance(bundle.interventions, InterventionRegistry)
        assert isinstance(bundle.intervention_handler, InterventionHandler)
        assert isinstance(bundle.intervention_coordinator, InterventionCoordinator)
        assert isinstance(bundle.chain_timeout_glue, ChainTimeoutGlue)

    # ── ★ the crux: intra-F7 references must be LOCAL, not self._X ───────

    def test_builder_does_not_crash_when_self_chains_is_unset(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ crux — reproduces the EXACT in-flight ``__init__``
        state the builder runs under (none of this family's five
        attributes assigned yet) and calls the builder directly. If ANY
        intra-family reference (``intervention_handler``'s ``registry=``,
        ``intervention_coordinator``'s ``registry=``/``handler=``, or
        ``chain_timeout_glue``'s ``chains=``) had been left as ``self._X``
        instead of the builder's LOCAL variables, this raises
        ``AttributeError`` — exactly the crash this extraction must avoid.
        Mirrors Family 5/6b's "does not crash before X exists" idiom for
        its own local-vs-self crux."""
        del session._chains
        del session._interventions
        del session._intervention_handler
        del session._intervention_coordinator
        del session._chain_timeout_glue

        bundle = session._build_intervention_bundle()

        assert isinstance(bundle, _InterventionBundle)

    # ── ★ intra-F7 wiring: coordinator/handler/glue point at THIS family's own instances ──

    def test_intra_family_wiring_points_at_this_family_s_own_instances(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ primary pin (architect spec) — after the builder
        runs, ``intervention_coordinator``'s ``registry``/``handler``,
        ``intervention_handler``'s ``registry``, and ``chain_timeout_
        glue``'s ``chains`` are all the SAME instances this family itself
        built — not fresh ones, and not left unset. These are the exact
        attributes the local-vs-self split targets — the extraction's own
        construction targets, not a faked collaborator's internals."""
        bundle = session._build_intervention_bundle()

        coordinator_registry = bundle.intervention_coordinator._registry
        coordinator_handler = bundle.intervention_coordinator._handler
        handler_registry = bundle.intervention_handler._registry
        glue_chains = bundle.chain_timeout_glue._chains

        assert coordinator_registry is bundle.interventions
        assert coordinator_handler is bundle.intervention_handler
        assert handler_registry is bundle.interventions
        assert glue_chains is bundle.chains

    # ── ★ F8→F7 cross-dep: self._chains must be set before InterAgentMessaging ──

    def test_inter_agent_messaging_chain_manager_is_the_same_chains_instance(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ F8-dep pin (architect spec) — Family 8's
        ``InterAgentMessaging`` (unmoved, constructed directly in
        ``__init__`` right after this builder returns) reads
        ``chain_manager=self._chains``. If the builder call had been
        placed AFTER ``InterAgentMessaging``'s construction (or if
        ``self._chains`` had not been assigned before that point), this
        cross-family dependency would have broken — either an
        ``AttributeError`` at ``__init__`` time, or (undetectably) a
        DIFFERENT ``ChainManager`` instance wired into
        ``InterAgentMessaging``. This proves it is the SAME instance."""
        from reyn.runtime.services.inter_agent_messaging import InterAgentMessaging

        inter_agent_messaging = session._inter_agent_messaging
        assert isinstance(inter_agent_messaging, InterAgentMessaging)
        wired_chains = inter_agent_messaging._chains
        session_chains = session._chains
        assert wired_chains is session_chains

    # ── deferred: chains → chain_timeout_glue resolves only via a bound method, at CALL time ──

    @pytest.mark.asyncio
    async def test_chain_timeout_fire_reaches_the_same_glue_via_deferred_bound_method(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ deferred pin — ``chains`` never holds a direct,
        eager reference to ``chain_timeout_glue`` (that would be the
        None-then-patch shape Family 6b needed and Family 7 explicitly
        does NOT). Instead, the reverse direction is wired through
        ``Session._on_chain_timeout_fire``, a bound method that forwards
        to ``self._chain_timeout_glue.on_chain_timeout_fire`` only when
        CALLED. This proves the forwarding reaches the SAME
        ``chain_timeout_glue`` instance this family built, resolved at
        call time (not frozen at construction time)."""
        called_with: list[str] = []

        async def _fake_on_chain_timeout_fire(chain_id: str) -> None:
            called_with.append(chain_id)

        session._chain_timeout_glue.on_chain_timeout_fire = _fake_on_chain_timeout_fire

        await session._on_chain_timeout_fire("probe-chain-id")

        assert called_with == ["probe-chain-id"]

    # ── strip-falsify: the identity checks themselves must be live ───────

    def test_strip_falsify_intra_family_wiring_checks_are_live(
        self, session: Session,
    ) -> None:
        """Tier 1: strip-falsify — a FRESH, independently-constructed
        ``InterventionRegistry`` (never passed into this family's real
        ``intervention_handler`` / ``intervention_coordinator``) must NOT
        be the same instance as the one those two components actually
        hold — proving the identity checks above are genuinely reading
        live wiring, not a check that would trivially pass regardless."""
        bundle = session._build_intervention_bundle()
        fresh_interventions = InterventionRegistry(
            on_announce=session._announce_intervention,
            enforce_listener_presence=True,
        )

        handler_registry = bundle.intervention_handler._registry
        coordinator_registry = bundle.intervention_coordinator._registry

        assert fresh_interventions is not bundle.interventions
        assert handler_registry is not fresh_interventions
        assert coordinator_registry is not fresh_interventions
        assert handler_registry is bundle.interventions
        assert coordinator_registry is bundle.interventions

    def test_strip_falsify_f8_dep_check_is_live(self, session: Session) -> None:
        """Tier 1: strip-falsify for the F8→F7 cross-dep pin — a FRESH,
        independently-constructed ``ChainManager`` must NOT be the same
        instance ``InterAgentMessaging`` actually holds — proving that
        pin is genuinely reading the live cross-family wiring."""
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
