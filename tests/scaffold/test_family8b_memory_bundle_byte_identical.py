# scaffold: triggered_by="#3082 Family 8b (memory bundle builder) extraction lands"
# scaffold: removed_by="The same PR that lands the extraction, once this test is green"
"""Tier 1: byte-identical characterization gate for the #3082 Family 8b
extraction — ``Session._build_memory_bundle`` pulling ``memory``
(``MemoryService``) out of ``Session.__init__`` into one builder returning
one typed bundle (``_MemoryBundle``).

Per the architect's #3082 Family 8 DAG correction (see the Family 8a
scaffold's docstring for the full grouping rationale), ``memory`` is one of
three mutually-independent leftover leaves (8a ``inter_agent_messaging``,
8b ``memory`` here, 8c ``mcp_connection_service``) straddling the
router-host WAIST (Family 6a) on both sides — each gets its own no-move,
single-component builder.

★ PRE-WAIST placement crux (the one thing that matters for this family):
``memory`` is PRE-WAIST — ``_build_router_waist`` (Family 6a) reads
``self._memory`` EAGERLY (``memory=self._memory``) when it constructs
``RouterHostAdapter``. This is the inverse direction of Family 7's F8→F7
``chains`` dependency (there, a LATER family reads an EARLIER family's
post-waist output; here, the WAIST builder itself reads THIS pre-waist
family's output) — so ``self._memory`` must be assigned before
``_build_router_waist`` runs. The builder call stays at its original,
unmoved position, well before the waist builder call, satisfying this.
This scaffold pins that ``RouterHostAdapter``'s wired memory IS the exact
same ``MemoryService`` instance ``Session._memory`` holds — proof the
waist builder picked up the pre-waist assignment rather than reading it
unset or reading a stale/fresh instance.

★ Cross-family EAGER dep: ``memory`` reads Family 1's ``chat_events``
(``events=self._chat_events``) eagerly at construction time (already set
on ``self`` by the time this builder runs, per the unmoved call-site
position).

Single independent leaf component (no deferred lambda, no intra-family
local-vs-self split — unlike Family 8a which has a deferred-lambda tail).

Per the extracted-refactor idiom (docs/deep-dives/contributing/testing.md
Annex: Scaffolding tests / CLAUDE.md's byte-identical-staged-externalization
rule), this scaffold is added and removed in the SAME PR that lands the
extraction, once green.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. Private-attribute
reads are resolved to a LOCAL variable on a line BEFORE the ``assert`` and
are the extraction's OWN target attributes (``memory._events`` /
``router_host._memory``) — Session's own state plus the exact wiring
targets this extraction's eager-dep and pre-waist-placement pins are about
(Family 4/5/6a/6b/7/8a's accepted idiom).
"""
from __future__ import annotations

import pytest

from reyn.runtime.services.memory_service import MemoryService
from reyn.runtime.session import Session, _MemoryBundle


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.chdir(tmp_path)
    return Session(agent_name="family8b-memory-test")


class TestFamily8bMemoryBundleByteIdentical:
    # ── builder contract ─────────────────────────────────────────────────

    def test_session_holds_the_real_type(self, session: Session) -> None:
        """Tier 1: the builder assigns a real ``MemoryService`` instance
        onto Session — the extraction's core contract."""
        memory = session._memory
        assert isinstance(memory, MemoryService)

    def test_builder_returns_a_memory_bundle(self, session: Session) -> None:
        """Tier 1: calling the builder directly (bound method on Session)
        returns a ``_MemoryBundle`` wrapping the real type — the builder's
        contract independent of ``__init__`` unpack wiring."""
        bundle = session._build_memory_bundle()
        assert isinstance(bundle, _MemoryBundle)
        assert isinstance(bundle.memory, MemoryService)

    # ── ★ cross-family EAGER dep: chat_events (F1) ────────────────────────

    def test_memory_events_is_the_same_chat_events_instance(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ F1→F8b cross-dep pin — ``memory`` reads
        ``events=self._chat_events`` EAGERLY at construction time. This
        proves it is the SAME ``EventLog`` instance Family 1 built, not a
        fresh one and not unset."""
        wired_events = session._memory._events
        session_events = session._chat_events
        assert wired_events is session_events

    # ── ★ pre-waist placement pin: RouterHostAdapter reads self._memory ──

    def test_router_host_memory_is_the_same_session_memory_instance(
        self, session: Session,
    ) -> None:
        """Tier 1: ★ pre-waist placement pin — ``_build_router_waist``
        (Family 6a) reads ``self._memory`` EAGERLY (``memory=self._memory``)
        when constructing ``RouterHostAdapter``. This proves the memory
        builder's call site genuinely runs BEFORE the waist builder call —
        the waist-built ``RouterHostAdapter`` holds the exact SAME
        ``MemoryService`` instance ``Session._memory`` holds, not an unset
        attribute (which would have raised ``AttributeError`` at
        construction) and not a different instance."""
        wired_memory = session._router_host._memory
        session_memory = session._memory
        assert wired_memory is session_memory

    # ── strip-falsify: the identity checks themselves must be live ───────

    def test_strip_falsify_events_check_is_live(self, session: Session) -> None:
        """Tier 1: strip-falsify for the F1→F8b cross-dep pin — a FRESH,
        independently-constructed ``MemoryService`` wired to a DIFFERENT
        ``EventLog`` must NOT be indistinguishable from the wired one,
        proving the events pin genuinely reads the live cross-family wiring
        rather than trivially passing regardless of what is wired."""
        from reyn.core.events.events import EventLog

        fresh_events = EventLog()
        fresh_memory = MemoryService(
            agent_workspace_dir=session.workspace_dir,
            events=fresh_events,
            file_write=session._file_write,
            file_read=session._file_read,
            file_delete=session._file_delete,
            file_regenerate_index=session._file_regenerate_index,
        )

        wired_events = session._memory._events
        session_events = session._chat_events
        fresh_memory_events = fresh_memory._events

        assert fresh_memory_events is not session_events
        assert wired_events is session_events
        assert wired_events is not fresh_events

    def test_strip_falsify_router_host_memory_check_is_live(
        self, session: Session,
    ) -> None:
        """Tier 1: strip-falsify for the pre-waist placement pin — a FRESH,
        independently-constructed ``MemoryService`` must NOT be the same
        instance ``session._router_host`` actually holds, proving the
        pre-waist pin genuinely reads the live waist wiring rather than
        trivially passing regardless of what was assigned before the waist
        builder ran."""
        fresh_memory = MemoryService(
            agent_workspace_dir=session.workspace_dir,
            events=session._chat_events,
            file_write=session._file_write,
            file_read=session._file_read,
            file_delete=session._file_delete,
            file_regenerate_index=session._file_regenerate_index,
        )

        wired_memory = session._router_host._memory
        session_memory = session._memory

        assert fresh_memory is not session_memory
        assert wired_memory is session_memory
        assert wired_memory is not fresh_memory


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
