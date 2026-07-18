# scaffold: triggered_by="#3082 Family 8c (mcp_connection_service bundle builder, FINAL family) extraction lands"
# scaffold: removed_by="The same PR that lands the extraction, once this test is green"
"""Tier 1: byte-identical characterization gate for the #3082 Family 8c
extraction (the FINAL family — landing this completes #3082's
``Session.__init__`` God-constructor decomposition) — pulling
``mcp_connection_service`` (``MCPConnectionService``) out of
``Session.__init__`` into one builder returning one typed bundle
(``_MCPConnectionBundle``).

★★ This family's crux — the sharpest deferred-resolution case in all of
Family 8 (4 refs, vs Family 5's 2): FOUR of the six keyword args
(``emit_sink`` / ``tools_cache_invalidate`` / ``hook_trigger`` /
``elicitation_gate``) are ``lambda`` closures that resolve
``self._chat_events`` (Family 1) / ``self._router_host`` (Family 6a) /
``self._hook_dispatcher`` (Family 3) / ``self._interventions`` (Family 7)
at CALL time — NONE of those four attributes exist yet at this builder's
call site (its original, unmoved position, ~:1511 — BEFORE all four
families construct their components). Eager-izing ANY of the four (the
Family 3/4 pattern, wrong HERE) would raise ``AttributeError`` the moment
the builder runs. This scaffold pins the inverse-pitfall directly: it
calls the bound builder on a real ``Session`` instance that has had all
four target attributes deliberately removed (mirroring the exact in-flight
state during ``__init__`` at this call site), proving construction is
crash-free, then re-attaches real components afterward and drives each of
the four closures through them — proving each re-resolves its target
``self.*`` attribute live, not a value snapshotted at builder-call time.

Only ``elicitation_bus=self.as_request_bus()`` and
``agent_name=self.agent_name`` are EAGER (both already resolvable at this
position — ``as_request_bus()`` needs only ``self``; ``self.agent_name`` is
backed by ``self._agent``, set much earlier in ``__init__``). This
scaffold pins both eager wirings too.

Single independent leaf component (unlike Family 6b/7's multi-component
families) — no intra-family local-vs-self split applies.

Per the extracted-refactor idiom (docs/deep-dives/contributing/testing.md
Annex: Scaffolding tests / CLAUDE.md's byte-identical-staged-externalization
rule), this scaffold is added and removed in the SAME PR that lands the
extraction, once green.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. Private-attribute
reads (``session._mcp_connection_service``, the MCP service's own
``_emit_sink``/``_tools_cache_invalidate``/``_hook_trigger``/
``_elicitation_gate``/``_elicitation_bus``/``_agent_name`` — the extraction's
OWN target attributes, exactly what this family's eager-vs-deferred split
is about) are resolved to a LOCAL variable on a line BEFORE any ``assert``,
mirroring Family 5/8a's accepted idiom, and are used ONLY to invoke the
closures / drive a PUBLIC-surface check on the next line — never asserted
on directly.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.events import EventLog
from reyn.hooks.bus import HookBus
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.registry import HookRegistry
from reyn.mcp.connection_service import MCPConnectionService
from reyn.runtime.services.intervention_registry import InterventionRegistry
from reyn.runtime.session import Session, _MCPConnectionBundle
from reyn.runtime.session_buses import AgentRequestBus


async def _noop_async(*_args: object, **_kwargs: object) -> None:
    return None


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.chdir(tmp_path)
    return Session(agent_name="family8c-mcp-connection-test")


class TestFamily8cMCPConnectionBundleByteIdentical:
    # ── builder contract ─────────────────────────────────────────────────

    def test_session_holds_the_real_type(self, session: Session) -> None:
        """Tier 1: the builder assigns a real ``MCPConnectionService``
        instance onto Session — the extraction's core contract."""
        mcp_connection_service = session._mcp_connection_service
        assert isinstance(mcp_connection_service, MCPConnectionService)

    def test_builder_returns_an_mcp_connection_bundle(self, session: Session) -> None:
        """Tier 1: calling the builder directly (public method on Session)
        returns an ``_MCPConnectionBundle`` wrapping the real component —
        the builder's contract independent of ``__init__`` unpack wiring."""
        bundle = session._build_mcp_connection_service()
        assert isinstance(bundle, _MCPConnectionBundle)
        assert isinstance(bundle.mcp_connection_service, MCPConnectionService)

    # ── eager wiring (the only 2 non-deferred args) ──────────────────────

    def test_agent_name_wired_eagerly(self, session: Session) -> None:
        """Tier 1: ``agent_name=self.agent_name`` — eager passthrough,
        resolved at builder-call time (no lambda)."""
        mcp_connection_service = session._mcp_connection_service
        agent_name = mcp_connection_service._agent_name
        assert agent_name == session.agent_name
        assert agent_name == "family8c-mcp-connection-test"

    def test_elicitation_bus_wired_eagerly_to_this_session(self, session: Session) -> None:
        """Tier 1: ``elicitation_bus=self.as_request_bus()`` — eager, wraps
        THIS session (proven via ``AgentRequestBus``'s public ``.session``
        accessor, never a private-attribute peek)."""
        mcp_connection_service = session._mcp_connection_service
        elicitation_bus = mcp_connection_service._elicitation_bus
        assert isinstance(elicitation_bus, AgentRequestBus)
        assert elicitation_bus.session is session

    # ── ★★ deferred crux: crash-free before all 4 components exist ───────

    def test_builder_does_not_crash_before_any_of_the_4_components_exist(
        self, session: Session,
    ) -> None:
        """Tier 1: the builder is invoked (in real ``__init__``) BEFORE
        ``self._chat_events`` / ``self._router_host`` /
        ``self._hook_dispatcher`` / ``self._interventions`` are assigned
        (Families 1/6a/3/7 all run later). This test reproduces that exact
        in-flight state on a REAL ``Session`` instance — deliberately
        removing all four target attributes — and calls the bound builder
        directly. Construction must NOT raise. If any of the four lambdas
        captured its target ``self.*`` attribute EAGERLY (the Family 3/4
        pattern misapplied here), this would raise ``AttributeError`` at
        this exact call — the crash this family's spec exists to prevent
        (the F5 inverse-pitfall, sharpened to 4 refs)."""
        del session._chat_events
        del session._router_host
        del session._hook_dispatcher
        del session._interventions
        has_chat_events_before = hasattr(session, "_chat_events")
        has_router_host_before = hasattr(session, "_router_host")
        has_hook_dispatcher_before = hasattr(session, "_hook_dispatcher")
        has_interventions_before = hasattr(session, "_interventions")

        bundle = session._build_mcp_connection_service()

        has_chat_events_after = hasattr(session, "_chat_events")
        has_router_host_after = hasattr(session, "_router_host")
        has_hook_dispatcher_after = hasattr(session, "_hook_dispatcher")
        has_interventions_after = hasattr(session, "_interventions")
        assert has_chat_events_before is False
        assert has_router_host_before is False
        assert has_hook_dispatcher_before is False
        assert has_interventions_before is False
        assert isinstance(bundle, _MCPConnectionBundle)
        assert isinstance(bundle.mcp_connection_service, MCPConnectionService)
        # still true: the builder never touches any of the 4 during construction.
        assert has_chat_events_after is False
        assert has_router_host_after is False
        assert has_hook_dispatcher_after is False
        assert has_interventions_after is False

    # ── ★★ deferred crux: each of the 4 lambdas re-resolves post-construction ──

    def test_emit_sink_resolves_chat_events_at_call_time(self, session: Session) -> None:
        """Tier 1: after the crash-free builder call above, assigning
        ``self._chat_events`` AFTER THE FACT (mirroring Family 1 running
        later in ``__init__``) makes ``emit_sink`` start landing events on
        it — proving live, per-call resolution rather than a value
        snapshotted at builder-call time."""
        del session._chat_events
        bundle = session._build_mcp_connection_service()
        session._chat_events = EventLog()  # Family 1 runs later in real __init__

        emit_sink = bundle.mcp_connection_service._emit_sink
        emit_sink("mcp_test_event", server="srv")

        chat_events = session._chat_events
        types = [e.type for e in chat_events.all()]
        assert "mcp_test_event" in types

    def test_tools_cache_invalidate_resolves_router_host_at_call_time(
        self, session: Session,
    ) -> None:
        """Tier 1: same deferred-resolution proof for
        ``tools_cache_invalidate`` — Family 6a's ``self._router_host`` is
        assigned AFTER the builder call, then invalidating through the
        closure resets the REAL router_host's cache (public
        ``mcp_tools_cache_snapshot`` surface, never a private peek in the
        assert)."""
        router_host = session._router_host  # real, already built by Family 6a
        del session._router_host
        bundle = session._build_mcp_connection_service()
        session._router_host = router_host  # Family 6a runs later in real __init__
        # Arrange: populate the cache directly (test setup, not an assert)
        # so invalidation has an observable before/after through the public
        # snapshot surface.
        router_host._mcp_tools_cache = {"srv": []}
        assert router_host.mcp_tools_cache_snapshot == {"srv": []}

        tools_cache_invalidate = bundle.mcp_connection_service._tools_cache_invalidate
        tools_cache_invalidate("srv")

        assert session._router_host.mcp_tools_cache_snapshot is None

    def test_hook_trigger_resolves_hook_dispatcher_at_call_time(
        self, session: Session,
    ) -> None:
        """Tier 1: same deferred-resolution proof for ``hook_trigger`` —
        a FRESH real ``HookDispatcher`` (with a real ``HookBus``, so the
        dispatch is observable without needing a matching hook registered —
        ``HookDispatcher.dispatch`` broadcasts to the bus UNCONDITIONALLY)
        is assigned to ``self._hook_dispatcher`` AFTER the builder call;
        driving the closure through it lands a real ``HookEvent`` on the
        bus subscription."""
        del session._hook_dispatcher
        bundle = session._build_mcp_connection_service()
        bus = HookBus()
        session._hook_dispatcher = HookDispatcher(
            HookRegistry([]),
            put_inbox=_noop_async,
            stage_next_turn_context=_noop_async,
            bus=bus,
        )
        subscription = bus.subscribe()

        hook_trigger = bundle.mcp_connection_service._hook_trigger
        coro = hook_trigger("mcp_resource_updated", {"server": "srv"})
        asyncio.run(coro)

        event = subscription.get_nowait()
        assert event.payload == {"server": "srv"}
        subscription.close()

    def test_elicitation_gate_resolves_interventions_at_call_time(
        self, session: Session,
    ) -> None:
        """Tier 1: same deferred-resolution proof for ``elicitation_gate``
        — a fresh real ``InterventionRegistry`` is assigned to
        ``self._interventions`` AFTER the builder call; the closure
        tracks its LIVE ``has_active_listener()`` state (False with no
        listener, True after one registers) through the real registry's
        public surface."""
        del session._interventions
        bundle = session._build_mcp_connection_service()
        session._interventions = InterventionRegistry(on_announce=_noop_async)

        elicitation_gate = bundle.mcp_connection_service._elicitation_gate
        gate_before = elicitation_gate()
        session._interventions.register_listener("test-listener")
        gate_after_register = elicitation_gate()
        session._interventions.unregister_listener("test-listener")
        gate_after_unregister = elicitation_gate()

        assert gate_before is False
        assert gate_after_register is True
        assert gate_after_unregister is False

    # ── strip-falsify: deferred wiring is live, not vacuous ──────────────

    def test_strip_falsify_elicitation_gate_reresolves_on_reassignment(
        self, session: Session,
    ) -> None:
        """Tier 1: strip-falsify — after wiring ``_interventions`` = registry
        A and confirming the gate reads A's live state, REASSIGN
        ``self._interventions`` to a DIFFERENT registry B and confirm the
        gate now reads B's state, not a value cached from A. This proves
        the closure re-reads ``self._interventions`` on every call
        (genuinely deferred) rather than closing over the first-seen
        object's snapshot — a check that would pass vacuously if the
        closure had captured ``interventions`` by value at builder-call
        time instead."""
        del session._interventions
        bundle = session._build_mcp_connection_service()
        registry_a = InterventionRegistry(on_announce=_noop_async)
        registry_b = InterventionRegistry(on_announce=_noop_async)
        elicitation_gate = bundle.mcp_connection_service._elicitation_gate

        session._interventions = registry_a
        registry_a.register_listener("a-listener")
        gate_reads_a = elicitation_gate()
        assert gate_reads_a is True

        session._interventions = registry_b  # registry_b has NO listener registered
        gate_reads_b = elicitation_gate()
        assert gate_reads_b is False, (
            "strip-falsify: the closure did not re-resolve self._interventions "
            "after reassignment — the deferred-wiring pin would be vacuous"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
