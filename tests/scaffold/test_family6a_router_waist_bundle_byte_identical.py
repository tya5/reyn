# scaffold: triggered_by="#3082 Family 6a (router_host waist bundle builder) extraction lands"
# scaffold: removed_by="The same PR that lands the extraction, once this test is green"
"""Tier 1: byte-identical characterization gate for the #3082 Family 6a
extraction — ``Session._build_router_waist`` pulling the ``RouterHostAdapter``
construction (the pipeline's WAIST: ~40 already-constructed Session
sub-components aggregated into one object) out of ``Session.__init__`` into
one builder returning one typed bundle (``_RouterWaistBundle``).

The load-bearing distinction from Families 1-5: this family's builder is an
instance method with NO explicit params at all (unlike Family 2's local
``state_log`` or Family 4/5's local ``chat_events``/``agent_name`` args) —
every one of ``RouterHostAdapter``'s ~40 dependencies is already an attribute
on ``self`` (or a bound method / property) by the time the builder runs, so
parameterizing them individually was judged impractical by the architect
spec. This scaffold pins two things behaviorally:

1. A representative sample of the ~40 eager wiring args — proven via
   ``RouterHostAdapter``'s OWN public accessors (``get_agent_registry()`` /
   ``get_pipeline_registry()`` / ``get_presentation_registry()`` / ``events``
   / ``state_log`` / ``permission_resolver`` / ``resolver`` / ``agent_name``
   / ``agent_role``), never a private-attribute peek into the adapter's
   internals — compared by identity/equality against the SAME Session
   attributes that fed the builder (which is this extraction's own target
   state, the accepted idiom per Family 4/5's ``isinstance(session._budget,
   BudgetGateway)`` shape).
2. ★ The 3 DEFERRED per-turn lambdas — ``live_session_id_fn`` /
   ``current_task_id_fn`` / ``turn_origin_fn`` — MUST keep resolving
   ``self._session_id`` / ``self._current_task_id`` / ``self._current_turn_
   origin`` at CALL time, not at builder-call time. Both
   ``_current_task_id`` (default ``None``) and ``_current_turn_origin``
   (default ``"auto_improvement"``) already carry a pre-turn default at
   construction, before the builder runs; they are REASSIGNED per turn
   inside ``run_one_iteration``, far after ``__init__`` returns. Proven via
   ``RouterHostAdapter``'s public surface: the ``live_session_id`` property
   and ``make_router_op_context().current_task_id`` /
   ``.turn_origin`` fields, read BEFORE and AFTER mutating the owning
   Session's per-turn state post-construction (mirroring exactly what
   ``run_one_iteration`` does at runtime). If any of the three were
   eager-ized (the Family 3/5 deferred/eager pitfall), the value read after
   the post-construction mutation would still show the STALE
   pre-turn/construction-time default — the strip-falsify shape this
   scaffold pins.

Per the extracted-refactor idiom (docs/deep-dives/contributing/testing.md
Annex: Scaffolding tests / CLAUDE.md's byte-identical-staged-externalization
rule), this scaffold is added and removed in the SAME PR that lands the
extraction, once green.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. Private-Session-attr
reads (``session._router_host``, ``session._registry`` etc.) are resolved to
a LOCAL variable on a line BEFORE the ``assert`` and are the extraction's own
target attribute / this Session's own state — not a faked collaborator.
"""
from __future__ import annotations

import pytest

from reyn.runtime.services.router_host_adapter import RouterHostAdapter
from reyn.runtime.session import Session, _RouterWaistBundle


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.chdir(tmp_path)
    return Session(agent_name="family6a-router-waist-test")


class TestFamily6aRouterWaistBundleByteIdentical:
    # ── builder contract ─────────────────────────────────────────────────

    def test_session_holds_router_host_of_real_type(self, session: Session) -> None:
        """Tier 1: the builder assigns a real ``RouterHostAdapter`` onto
        ``Session._router_host`` — the extraction's core contract."""
        router_host = session._router_host
        is_adapter = isinstance(router_host, RouterHostAdapter)
        assert is_adapter

    def test_builder_returns_a_router_waist_bundle(self, session: Session) -> None:
        """Tier 1: calling the builder directly (a public-ish bound method on
        Session) returns a ``_RouterWaistBundle`` wrapping a real
        ``RouterHostAdapter`` — the builder's contract independent of
        ``__init__`` unpack wiring."""
        bundle = session._build_router_waist()
        is_bundle = isinstance(bundle, _RouterWaistBundle)
        is_adapter = isinstance(bundle.router_host, RouterHostAdapter)
        assert is_bundle
        assert is_adapter

    # ── representative sample of the ~40 eager aggregate wires ───────────

    def test_agent_registry_pipeline_registry_presentation_registry_wired(
        self, session: Session,
    ) -> None:
        """Tier 1: three of the Family-1-5-adjacent registries router_host
        aggregates are the SAME instances Session holds (byte-identical
        eager wiring, not fresh copies) — proven via router_host's own
        public accessors."""
        router_host = session._router_host
        wired_agent_registry = router_host.get_agent_registry()
        wired_pipeline_registry = router_host.get_pipeline_registry()
        wired_presentation_registry = router_host.get_presentation_registry()
        assert wired_agent_registry is session._registry
        assert wired_pipeline_registry is session._pipeline_registry
        assert wired_presentation_registry is session._presentation_registry

    def test_events_state_log_permission_resolver_resolver_wired(
        self, session: Session,
    ) -> None:
        """Tier 1: the Family-1 EventLog / recovery WAL / permission
        resolver / model resolver are the SAME instances (byte-identical
        eager wiring), proven via router_host's own public properties."""
        router_host = session._router_host
        wired_events = router_host.events
        wired_state_log = router_host.state_log
        wired_permission_resolver = router_host.permission_resolver
        wired_resolver = router_host.resolver
        assert wired_events is session._chat_events
        assert wired_state_log is session._state_log
        assert wired_permission_resolver is session._perm
        assert wired_resolver is session._resolver

    def test_agent_name_and_role_passthrough(self, session: Session) -> None:
        """Tier 1: ``agent_name``/``agent_role`` passthrough — proven via
        router_host's own public properties, not hardcoded on the bundle."""
        router_host = session._router_host
        wired_agent_name = router_host.agent_name
        wired_agent_role = router_host.agent_role
        assert wired_agent_name == session.agent_name
        assert wired_agent_role == session._agent_role

    # ── ★ the 3 deferred per-turn lambdas: crux of this family ───────────

    def test_live_session_id_resolves_at_call_time_not_construction_time(
        self, session: Session,
    ) -> None:
        """Tier 1 / ★ crux: ``live_session_id_fn=lambda: self._session_id``
        must be DEFERRED — reassigning ``session._session_id`` AFTER
        construction (mirroring a spawned session's post-construction sid
        stamp) must show up on ``router_host.live_session_id`` immediately.
        If the lambda had been eager-ized to a fixed value at builder-call
        time, this would still show the OLD sid — the strip-falsify shape."""
        router_host = session._router_host
        original_live_sid = router_host.live_session_id
        assert original_live_sid == session._session_id

        session._session_id = "spawned-session-live-sid"

        updated_live_sid = router_host.live_session_id
        assert updated_live_sid == "spawned-session-live-sid", (
            "strip-falsify: live_session_id_fn did not re-resolve "
            "self._session_id after reassignment — eager-ized, not deferred"
        )

    def test_current_task_id_and_turn_origin_resolve_at_call_time(
        self, session: Session,
    ) -> None:
        """Tier 1 / ★ crux: ``current_task_id_fn`` / ``turn_origin_fn`` must
        be DEFERRED per-turn callbacks, NOT frozen at construction. Both
        ``_current_task_id`` (default ``None``) and ``_current_turn_origin``
        (default ``"auto_improvement"``) already carry a PRE-TURN default at
        construction time, BEFORE the builder runs — proven via
        ``make_router_op_context()``'s ``current_task_id`` / ``turn_origin``
        fields reflecting exactly those pre-turn defaults on a fresh
        session, then reflecting freshly-set per-turn values immediately
        after mutation (mirroring exactly what a real turn does in
        ``run_one_iteration``). An eager-captured lambda would keep showing
        the PRE-TURN default forever, even after a real turn reassigns it —
        the strip-falsify shape this pins."""
        router_host = session._router_host

        ctx_before = router_host.make_router_op_context()
        assert ctx_before.current_task_id is None
        assert ctx_before.turn_origin == "auto_improvement"

        session._current_task_id = "task-42"
        session._current_turn_origin = "user_directed"

        ctx_after = router_host.make_router_op_context()
        assert ctx_after.current_task_id == "task-42", (
            "strip-falsify: current_task_id_fn did not re-resolve "
            "self._current_task_id after the turn set it — eager-ized, not deferred"
        )
        assert ctx_after.turn_origin == "user_directed", (
            "strip-falsify: turn_origin_fn did not re-resolve "
            "self._current_turn_origin after the turn set it — eager-ized, not deferred"
        )

    def test_strip_falsify_current_task_id_reresolves_on_second_reassignment(
        self, session: Session,
    ) -> None:
        """Tier 1: strip-falsify — after observing task-42 flow through,
        reassign to a DIFFERENT task id and confirm the NEW value flows
        through too. This proves the lambda re-reads
        ``self._current_task_id`` on EVERY call (genuinely deferred) rather
        than caching the first-seen value, which would pass the single-
        mutation check above vacuously."""
        router_host = session._router_host

        session._current_task_id = "task-a"
        ctx_a = router_host.make_router_op_context()
        assert ctx_a.current_task_id == "task-a"

        session._current_task_id = "task-b"
        ctx_b = router_host.make_router_op_context()
        assert ctx_b.current_task_id == "task-b", (
            "strip-falsify: current_task_id_fn cached the first-seen value "
            "instead of re-resolving self._current_task_id on every call"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
