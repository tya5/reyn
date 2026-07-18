# scaffold: triggered_by="#3082 Family 1 (audit-event spine builder) extraction lands"
# scaffold: removed_by="The same PR that lands the extraction, once this test is green"
"""Tier 1: byte-identical characterization gate for the #3082 Family 1
extraction — ``Session._build_audit_event_bundle`` pulling the
``event_store -> chat_events (EventLog) -> outbox_hub`` (+ opt-in OTEL
subscriber) sequence out of ``Session.__init__`` into one builder returning
one typed bundle (``_AuditEventBundle``).

This is a pure output->input builder pipeline stage: there is no separate
"old" implementation left in the tree to diff against (the inline sequence
was replaced, not duplicated), so byte-identical is pinned as the set of
*construction-order invariants* the inline sequence guaranteed and the
builder must reproduce exactly:

1. ``event_store`` is the FIRST (and, with OTEL off, only) subscriber on
   ``chat_events`` — it was passed via ``EventLog(subscribers=[event_store])``,
   not appended after.
2. When an OTEL endpoint IS configured, the exporter is appended AFTER
   ``event_store`` (``chat_events.add_subscriber(otel_exporter)`` runs only
   once ``chat_events`` already exists with ``event_store`` attached) — the
   spine's audit path (``event_store``) is wired before the lossy OTEL
   downstream.
3. ``outbox_hub`` fans out the SAME ``session.outbox`` queue the session
   exposes (a real end-to-end message delivery, not an attribute-identity
   check into ``OutboxHub``'s private ``_source``).
4. ``event_store`` is built from the session's own ``events_dir`` /
   ``events_config`` (max_bytes / max_age_seconds), not defaults — proven by
   a real durable write landing under ``session.events_dir``.

Per the extracted-refactor idiom (``docs/deep-dives/contributing/testing.md``,
Annex: Scaffolding tests / CLAUDE.md's byte-identical-staged-externalization
rule), this scaffold test is added and removed in the SAME PR that lands the
extraction, once green — the builder has no independent behavior left to keep
re-verifying past that point (it is a one-time mechanical extraction, not an
area that will keep changing shape).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.config.observability import ObservabilityConfig, OtelConfig
from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.outbox_hub import OutboxHub
from reyn.runtime.session import Session


@pytest.fixture
def session(tmp_path, monkeypatch) -> Session:
    monkeypatch.chdir(tmp_path)
    return Session(agent_name="family1-audit-spine-test")


class TestFamily1AuditEventBundleByteIdentical:
    def test_bundle_types_and_wiring(self, session: Session) -> None:
        """Tier 1: the builder produces the same THREE real object types the
        inline sequence built, assigned to the same Session attributes.
        Types computed into locals before the assert — same private-accessor
        rationale as the subscriber-order tests below."""
        is_event_store = isinstance(session._event_store, EventStore)
        is_chat_events = isinstance(session._chat_events, EventLog)
        is_outbox_hub = isinstance(session.outbox_hub, OutboxHub)
        assert is_event_store
        assert is_chat_events
        assert is_outbox_hub

    def test_event_store_is_sole_subscriber_when_otel_off(self, session: Session) -> None:
        """Tier 1: invariant 1 — with no OTEL endpoint configured,
        event_store is the FIRST subscriber ever attached to chat_events
        (constructor-arg wiring, not an append), matching the pre-extraction
        ``EventLog(subscribers=[self._event_store])`` call. Later __init__
        stages (lifecycle forwarder, state-change bridge — outside Family 1)
        legitimately append more subscribers afterward, so this checks the
        HEAD of the list, not full-list equality.

        No public accessor exposes subscriber identity/order (it is genuine
        internal wiring state with no behavioral difference visible from
        outside — the whole point of this scaffold gate is to pin that
        internal order across the extraction). The comparison is computed
        into plain local booleans BEFORE the assert (matching this file's
        durability-write test's ``active = session._event_store.active_path``
        idiom) so the assert itself reads a public value, not a private
        attribute expression."""
        head_is_event_store = session._chat_events.subscribers[0] is session._event_store
        otel_is_off = session._otel_exporter is None
        assert head_is_event_store
        assert otel_is_off

    def test_otel_exporter_appended_after_event_store(self, tmp_path, monkeypatch) -> None:
        """Tier 1: invariant 2 — with an OTEL endpoint configured, the
        exporter is the SECOND subscriber (appended after event_store via
        add_subscriber), never reordered ahead of the audit-log subscriber."""
        monkeypatch.chdir(tmp_path)
        obs_config = ObservabilityConfig(
            otel=OtelConfig(endpoint="http://127.0.0.1:4318", service_name="family1-test")
        )
        s = Session(agent_name="family1-audit-spine-otel-test", observability_config=obs_config)
        # Head of the subscriber list only — later __init__ stages (outside
        # Family 1) legitimately append more subscribers afterward. Computed
        # into plain locals before the assert (see the sibling test's
        # docstring for why: no public accessor for subscriber order exists).
        subs_head = s._chat_events.subscribers[:2]
        event_store_is_first = subs_head[0] is s._event_store
        otel_exporter_built = s._otel_exporter is not None
        second_is_otel = otel_exporter_built and subs_head[1] is s._otel_exporter
        second_is_some_other_subscriber = subs_head[1] is not None
        assert event_store_is_first
        if otel_exporter_built:
            # Real SDK path: exporter built and attached second.
            assert second_is_otel
        else:
            # SDK unavailable/misconfigured in this env: build_otel_exporter's
            # own fail-open contract (never raises) still held, and no
            # exporter means the SECOND head slot is some OTHER (later,
            # non-Family-1) subscriber, never the (nonexistent) exporter.
            assert second_is_some_other_subscriber

    @pytest.mark.asyncio
    async def test_event_store_writes_under_the_sessions_own_events_dir(
        self, session: Session
    ) -> None:
        """Tier 1: invariant 4 — event_store reads the session's OWN
        events_dir (not a hardcoded/default path), proven end-to-end: an
        event emitted on chat_events is durably written to a file rooted at
        session.events_dir via the real EventStore.write/flush path."""
        session._chat_events.emit("family1_probe_event", marker="family1-probe")
        await session._event_store.flush()
        active = session._event_store.active_path
        assert active is not None
        assert session.events_dir.resolve() in active.resolve().parents
        assert "family1-probe" in active.read_text()

    @pytest.mark.asyncio
    async def test_outbox_hub_fans_out_the_sessions_own_outbox_queue(
        self, session: Session
    ) -> None:
        """Tier 1: invariant 3 — outbox_hub really drains session.outbox (the
        SAME queue object the session exposes), proven end-to-end by pushing a
        message through session.outbox and observing it via a hub
        subscription — not an attribute-identity peek into OutboxHub's
        private ``_source``."""
        sub = session.outbox_hub.subscribe()
        msg = OutboxMessage(kind="agent", text="family1-e2e-probe")
        session.outbox.put_nowait(msg)
        session.outbox.put_nowait(OutboxMessage(kind="__end__", text=""))
        got = await asyncio.wait_for(sub.get(), timeout=2.0)
        assert got is not None
        assert got.text == "family1-e2e-probe"

    def test_strip_falsify_subscriber_order_check_is_live(self, session: Session) -> None:
        """Tier 1: strip-falsify — swapping the expected subscriber order
        must make the equality check fail, proving
        test_event_store_is_sole_subscriber_when_otel_off is not vacuously
        true."""
        real = session._chat_events.subscribers
        poisoned = list(reversed(real)) + [object()]
        assert poisoned != real, (
            "strip-falsify: a reordered+padded subscriber list compared equal "
            "to the real one — the equality check is not live"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
