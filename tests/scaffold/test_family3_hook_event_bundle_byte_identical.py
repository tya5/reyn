# scaffold: triggered_by="#3082 Family 3 (hook-event / reactivity bundle builder) extraction lands"
# scaffold: removed_by="The same PR that lands the extraction, once this test is green"
"""Tier 1: byte-identical characterization gate for the #3082 Family 3
extraction — ``Session._build_hook_event_bundle`` pulling the six-element
hook-event / reactivity spine (``hook_bus`` -> ``hook_dispatcher`` ->
``fs_watcher`` -> ``composer_registry`` -> ``composed_consumer`` ->
``hot_reloader``) out of ``Session.__init__`` into one builder returning one
typed bundle (``_HookEventBundle``).

This is a pure output->input builder pipeline stage: there is no separate
"old" implementation left in the tree to diff against (the inline sequence
was replaced, not duplicated), so byte-identical is pinned as the set of
*intra-family wiring invariants* the inline sequence guaranteed and the
builder must reproduce exactly — pinned BEHAVIORALLY (drive the wired object,
observe the downstream family effect) rather than by peeking at private
identity, per the Family 2 lesson:

1. ``hook_dispatcher`` is wired to the family's ``hook_bus`` — a
   ``dispatch()`` publishes onto the very bus a subscriber attached to
   ``session._hook_bus`` observes.
2. ``composed_consumer`` bridges family ``hook_bus`` events to the family
   ``hook_dispatcher`` — a ``composed:*`` event published onto
   ``session._hook_bus`` runs a Sync ``on: composed:*`` hook registered on
   ``session._hook_dispatcher`` (proving BOTH ``consumer.bus IS hook_bus``
   AND ``consumer.dispatcher IS hook_dispatcher`` end-to-end).
3. ``fs_watcher``'s deferred ``hook_trigger`` lambda reaches the family
   ``hook_dispatcher`` — the family-specific deferred wiring (the lambda
   closes over ``self._hook_dispatcher``, which is unpacked onto ``self``
   only AFTER the builder returns): invoking the wired trigger publishes onto
   ``session._hook_bus``.
4. ``hook_bus``'s ``emit_event`` sink reaches the family ``chat_events`` — a
   forced subscriber-queue drop fires a ``bus_subscriber_dropped`` P6
   audit-event that lands in ``session._chat_events``.
5. ``hot_reloader.events IS chat_events`` (the value-dependency at the heart
   of the pre-impl-caught crash: ``hot_reloader`` reads ``chat_events``
   EAGERLY at construction, which is why the family is built AFTER Family 1)
   — a ``hot_reloader`` reload emits a ``config_reloaded`` event that lands in
   ``session._chat_events``.
6. ``get_active_hot_reloader()`` publishes THIS session's ``hot_reloader``.

Strip-falsify (invariant 2 is live / non-vacuous): re-running invariant 2's
routing against a ``composed_consumer`` deliberately wired to a DIFFERENT bus
observes NO dispatch — proving the routing observed in
``test_composed_consumer_bridges_bus_events_to_the_family_dispatcher`` genuinely
depends on the consumer being wired to ``session._hook_bus``, not a check that
would pass against any bus.

No new WAL-derived recovery state is introduced by this extraction (pure move
of existing construction code), so the CLAUDE.md truncate-falsify
recovery-feature PR gate does not apply; what this scaffold instead pins is
that the hook-event WIRING itself survives the extraction byte-identically —
including the reordering (fs_watcher / hook_bus move ~200+ lines down into the
builder, and the whole family moves AFTER the Family 1 chat_events
assignment). Per the extracted-refactor idiom
(``docs/deep-dives/contributing/testing.md`` Annex: Scaffolding tests /
CLAUDE.md's byte-identical-staged-externalization rule), this scaffold is
added and removed in the SAME PR that lands the extraction, once green.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.hooks.bus import HookBus
from reyn.hooks.composed_consumer import ComposedEventConsumer
from reyn.hooks.composer import ComposerRegistry
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.event import HookEvent
from reyn.runtime.fs_watcher import FsWatcher
from reyn.runtime.hot_reload import HotReloader, get_active_hot_reloader
from reyn.runtime.session import Session, _HookEventBundle

_TIMEOUT = 2.0  # bounds every await so a broken wiring fails RED, never hangs


def _make_session(
    tmp_path: Path,
    *,
    name: str = "family3-hook-event-test",
    hooks_config: "list | None" = None,
    composers_config: "list | None" = None,
) -> Session:
    return Session(
        agent_name=name,
        state_log=StateLog(tmp_path / f"{name}.wal"),
        snapshot_path=tmp_path / f"{name}.json",
        hooks_config=hooks_config,
        composers_config=composers_config,
    )


async def _wait_until(pred, *, timeout: float = _TIMEOUT) -> None:
    """Poll ``pred`` to True within ``timeout`` (a broken wiring never
    satisfies it, so the caller's assertion fails RED)."""
    async def _loop() -> None:
        while not pred():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_loop(), timeout=timeout)


class TestFamily3HookEventBundleByteIdentical:
    def test_session_holds_six_hook_event_attrs_of_real_types(self, tmp_path) -> None:
        """Tier 1: the builder assigns the same SIX real object types the
        inline sequence built, on the same Session attributes."""
        s = _make_session(tmp_path)
        is_hook_bus = isinstance(s._hook_bus, HookBus)
        is_dispatcher = isinstance(s._hook_dispatcher, HookDispatcher)
        is_fs_watcher = isinstance(s._fs_watcher, FsWatcher)
        is_composer_registry = isinstance(s._composer_registry, ComposerRegistry)
        is_composed_consumer = isinstance(s._composed_consumer, ComposedEventConsumer)
        is_hot_reloader = isinstance(s._hot_reloader, HotReloader)
        assert is_hook_bus
        assert is_dispatcher
        assert is_fs_watcher
        assert is_composer_registry
        assert is_composed_consumer
        assert is_hot_reloader

    def test_builder_returns_a_hook_event_bundle(self, tmp_path) -> None:
        """Tier 1: calling the builder directly (public method on Session)
        returns a ``_HookEventBundle`` with the six expected real object types
        — the builder's contract independent of ``__init__`` unpack wiring."""
        from reyn.config.infra import FsWatchConfig

        s = _make_session(tmp_path)
        bundle = s._build_hook_event_bundle(
            {},                        # boot_in_set
            [],                        # composer_defs
            FsWatchConfig(),           # fs_watch_cfg
            s._chat_events,            # chat_events (Family 1 EventLog)
            s._registry,              # registry
            "family3-direct-sid",      # session_id
        )
        assert isinstance(bundle, _HookEventBundle)
        assert isinstance(bundle.hook_bus, HookBus)
        assert isinstance(bundle.hook_dispatcher, HookDispatcher)
        assert isinstance(bundle.fs_watcher, FsWatcher)
        assert isinstance(bundle.composer_registry, ComposerRegistry)
        assert isinstance(bundle.composed_consumer, ComposedEventConsumer)
        assert isinstance(bundle.hot_reloader, HotReloader)

    @pytest.mark.asyncio
    async def test_dispatcher_publishes_to_the_family_hook_bus(self, tmp_path) -> None:
        """Tier 1: invariant 1 — ``hook_dispatcher.bus IS hook_bus``. A
        ``dispatch()`` on the family dispatcher publishes onto the family bus
        (``dispatch()`` broadcasts to ``self._bus`` unconditionally), observed
        via ``session._hook_bus``'s own public subscribe() surface — not a
        ``dispatcher._bus`` identity peek."""
        s = _make_session(tmp_path)
        sub = s._hook_bus.subscribe()
        try:
            await s._hook_dispatcher.dispatch("turn_end", {"chain_id": "disp-probe"})
            event = await asyncio.wait_for(sub.get(), timeout=_TIMEOUT)
            assert event.payload["chain_id"] == "disp-probe"
        finally:
            sub.close()

    @pytest.mark.asyncio
    async def test_composed_consumer_bridges_bus_events_to_the_family_dispatcher(
        self, tmp_path
    ) -> None:
        """Tier 1: invariant 2 — ``composed_consumer.bus IS hook_bus`` AND
        ``composed_consumer.dispatcher IS hook_dispatcher``, proven end-to-end:
        a ``composed:*`` event published onto ``session._hook_bus`` is bridged
        by the family ``composed_consumer`` into the family
        ``hook_dispatcher``, which runs a Sync ``on: composed:*`` hook whose
        wake=true push lands on the Session's public inbox. Observed via the
        inbox's public ``qsize()`` — not a private wiring peek."""
        hooks_config = [
            {"on": "composed:family3_probe",
             "template_push": {"message": "composed probe", "wake": True}},
        ]
        # a producing composer is required (the #2889 gate rejects an
        # ``on: composed:X`` hook naming a kind no composer emits).
        composers_config = [
            {"name": "family3_probe", "op": "any",
             "inputs": [{"kind": "builtin:external:file_changed"}],
             "emit": {"kind": "composed:family3_probe"}},
        ]
        s = _make_session(tmp_path, hooks_config=hooks_config, composers_config=composers_config)
        before = s.inbox.qsize()
        s._composed_consumer.start()
        try:
            # publish must land AFTER the consumer's subscription is live, or
            # HookBus's broadcast-only publish silently drops it.
            await _wait_until(lambda: s._hook_bus.subscriber_count >= 1)
            s._hook_bus.publish(
                HookEvent(kind="composed:family3_probe", payload={"inputs": [], "correlation_key": "k"}),
            )
            await _wait_until(lambda: s.inbox.qsize() > before)
        finally:
            await s._composed_consumer.stop()
        assert s.inbox.qsize() > before

    @pytest.mark.asyncio
    async def test_fs_watcher_hook_trigger_reaches_the_family_dispatcher(
        self, tmp_path
    ) -> None:
        """Tier 1: invariant 3 — the family ``fs_watcher``'s deferred
        ``hook_trigger`` lambda reaches the family ``hook_dispatcher`` (whose
        ``dispatch()`` publishes onto the family ``hook_bus``). This is the
        family-specific DEFERRED wiring: the lambda closes over
        ``self._hook_dispatcher``, assigned onto ``self`` only AFTER the
        builder returns, so this pins that the deferred reference resolves to
        the SAME dispatcher the family built. Invoking the wired trigger
        callable + observing the family bus is behavioral (not an identity
        peek); a real-file-write drive (``test_2608_h4_fs_watcher.py``) is the
        full public route but needs watchdog + is slow/flaky, unsuited to a
        wiring scaffold."""
        s = _make_session(tmp_path)
        sub = s._hook_bus.subscribe()
        try:
            await s._fs_watcher._hook_trigger("turn_end", {"chain_id": "fs-probe"})
            event = await asyncio.wait_for(sub.get(), timeout=_TIMEOUT)
            assert event.payload["chain_id"] == "fs-probe"
        finally:
            sub.close()

    @pytest.mark.asyncio
    async def test_hook_bus_emit_event_reaches_family_chat_events(self, tmp_path) -> None:
        """Tier 1: invariant 4 — the family ``hook_bus``'s ``emit_event`` sink
        is wired to the family ``chat_events``. Forcing a subscriber-queue
        overflow (publish past the subscriber maxsize without draining) makes
        the bus drop its oldest entry and — on the first drop — fire a
        metadata-only ``bus_subscriber_dropped`` P6 audit-event through
        ``emit_event``; that event lands in ``session._chat_events``, observed
        via the EventLog's public ``all()`` read."""
        s = _make_session(tmp_path)
        # Attach ONE never-drained subscriber, then overflow it: HookBus's
        # default subscriber queue is bounded (128), so >128 undrained
        # publishes guarantees at least one drop.
        _sub = s._hook_bus.subscribe()
        try:
            for i in range(200):
                s._hook_bus.publish(HookEvent(kind="turn_end", payload={"i": i}))
            types = [e.type for e in s._chat_events.all()]
            assert "bus_subscriber_dropped" in types
        finally:
            _sub.close()

    @pytest.mark.asyncio
    async def test_hot_reloader_events_is_the_family_chat_events(self, tmp_path) -> None:
        """Tier 1: invariant 5 — ``hot_reloader.events IS chat_events`` (the
        value-dependency whose omission from the original spec's safety
        enumeration would have crashed construction had the builder run before
        the Family 1 chat_events assignment). Proven behaviorally: a
        ``hot_reloader`` reload emits a ``config_reloaded`` P6 event through
        its ``events`` sink, which lands in ``session._chat_events`` (EventLog
        ``all()``) iff the sink IS that EventLog."""
        s = _make_session(tmp_path)
        before = len(s._chat_events.all())
        await s._hot_reloader.apply_all(exclude=frozenset({"cron"}))
        after = [e.type for e in s._chat_events.all()]
        assert len(after) > before
        assert "config_reloaded" in after

    def test_get_active_hot_reloader_is_this_sessions_reloader(self, tmp_path) -> None:
        """Tier 1: invariant 6 — the builder-produced ``hot_reloader`` is
        published process-wide via ``set_active_hot_reloader`` (the side effect
        that moved WITH the family to after the builder call), so
        ``get_active_hot_reloader()`` returns THIS session's instance."""
        s = _make_session(tmp_path)
        this_sessions_reloader = s._hot_reloader
        assert get_active_hot_reloader() is this_sessions_reloader

    @pytest.mark.asyncio
    async def test_composer_registry_composers_subscribe_to_the_family_hook_bus(
        self, tmp_path
    ) -> None:
        """Tier 1: ``composer.bus IS hook_bus`` — a started family Composer
        subscribes to the family ``hook_bus``, observed via the bus's public
        ``subscriber_count``. Uses a real composers_config so the family
        registry actually holds a Composer."""
        composers_config = [
            {
                "name": "family3_composer",
                "op": "any",
                "inputs": [{"kind": "builtin:external:file_changed"}],
                "emit": {"kind": "composed:family3_composed"},
            }
        ]
        s = _make_session(tmp_path, composers_config=composers_config)
        before = s._hook_bus.subscriber_count
        s._composer_registry.start()
        try:
            # the Composer subscribed to THIS session's bus (composer.bus IS
            # hook_bus); _wait_until raises RED if the count never grows. Read
            # the peak WHILE the composer is live — stop() unsubscribes it.
            await _wait_until(lambda: s._hook_bus.subscriber_count > before)
            peak = s._hook_bus.subscriber_count
        finally:
            await s._composer_registry.stop()
        assert peak > before

    @pytest.mark.asyncio
    async def test_strip_falsify_composed_routing_is_live(self, tmp_path) -> None:
        """Tier 1: strip-falsify — re-run invariant 2's routing with a
        ``composed_consumer`` deliberately wired to a DIFFERENT (fresh) bus
        instead of ``session._hook_bus``. Publishing the same composed event
        onto ``session._hook_bus`` must NOT reach the dispatcher (the fresh-bus
        consumer never observes it), so the inbox does NOT grow — proving the
        real routing test genuinely depends on the consumer being wired to
        ``session._hook_bus`` (non-vacuous)."""
        hooks_config = [
            {"on": "composed:family3_probe",
             "template_push": {"message": "composed probe", "wake": True}},
        ]
        composers_config = [
            {"name": "family3_probe", "op": "any",
             "inputs": [{"kind": "builtin:external:file_changed"}],
             "emit": {"kind": "composed:family3_probe"}},
        ]
        s = _make_session(tmp_path, hooks_config=hooks_config, composers_config=composers_config)
        before = s.inbox.qsize()
        # A consumer wired to a FRESH bus but the SAME (real) dispatcher — the
        # single wiring under falsification is consumer.bus.
        other_bus = HookBus()
        poisoned = ComposedEventConsumer(bus=other_bus, dispatcher=s._hook_dispatcher)
        poisoned.start()
        try:
            await _wait_until(lambda: other_bus.subscriber_count >= 1)
            s._hook_bus.publish(
                HookEvent(kind="composed:family3_probe", payload={"inputs": [], "correlation_key": "k"}),
            )
            # give any (wrongly) live routing a chance to fire, then confirm none did
            await asyncio.sleep(0.2)
            assert s.inbox.qsize() == before, (
                "strip-falsify: a composed event on session._hook_bus reached the "
                "dispatcher through a consumer wired to an UNRELATED bus — the "
                "routing check would be vacuous"
            )
        finally:
            await poisoned.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
