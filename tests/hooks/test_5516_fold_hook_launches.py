"""Tier 2: #5516 — "1 メッセージずつ hook 起動する意味ないでしょ" (owner). N
queued hook-events fold into ONE launch carrying all N, instead of N
separate launches — end-to-end through the REAL ``HookDispatcher`` +
``_BoundedEventBridge``/``ComposedEventConsumer``, not just the isolated
``reyn.hooks.fold.drain_folded`` unit (see ``test_5516_fold_drain.py`` for
that). Real ``HookDispatcher``/``HookRegistry``/``HookDef``/
``McpIngressAdapter``/``HookBus``/``ComposedEventConsumer`` — recording
async callables for the injected seams (the established DI shape this
module's tests already use, per ``test_hook_dispatcher_1800_5b.py``'s own
policy note), no ``MagicMock``/``AsyncMock``."""
from __future__ import annotations

import asyncio

import pytest

from reyn.hooks.bus import HookBus
from reyn.hooks.composed_consumer import ComposedEventConsumer
from reyn.hooks.composer import COMPOSED_KIND_PREFIX
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.event import HookEvent
from reyn.hooks.ingress import McpIngressAdapter
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef, PushBlock


class _Recorder:
    """A real recording async callable — captures (args, kwargs) per call,
    no mock (mirrors test_hook_dispatcher_1800_5b.py's own helper)."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


async def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.01) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_poll(), timeout=timeout)


def _make_dispatcher(hooks: list[HookDef], **seams) -> tuple[HookDispatcher, dict]:
    seams.setdefault("put_inbox", _Recorder())
    seams.setdefault("stage_next_turn_context", _Recorder())
    seams.setdefault("run_shell", _Recorder())
    disp = HookDispatcher(
        HookRegistry(hooks),
        put_inbox=seams["put_inbox"],
        stage_next_turn_context=seams["stage_next_turn_context"],
        run_shell=seams["run_shell"],
        bus=seams.get("bus"),
        launch_pipeline=seams.get("launch_pipeline"),
    )
    return disp, seams


# ---------------------------------------------------------------------------
# exec/exec_capture: N queued events -> ONE run_shell call, array event_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_burst_of_mcp_events_folds_into_one_exec_launch_via_the_real_bridge():
    """Tier 2: LOAD-BEARING — the exact shape the issue's own driving
    measurement named (98 launches for 97 mcp_resource_updated events).
    A burst delivered to the REAL McpIngressAdapter's bridge, entirely
    before the drain task gets a scheduling chance, must produce exactly
    ONE ``run_shell`` call carrying all N payloads — never N calls."""
    hook = HookDef(on="mcp_resource_updated", exec=("echo", "hi"))
    disp, seams = _make_dispatcher([hook])

    burst_size = 5
    adapter = McpIngressAdapter(
        hook_trigger=disp.dispatch_external_batch, maxsize=32,
    )
    try:
        events = [
            adapter.to_event(f"file:///{i}", server="s", agent_name=None, resync=False)
            for i in range(burst_size)
        ]
        for event in events:
            adapter.deliver(event)  # all synchronous, no await between — one burst

        await _wait_for(lambda: len(seams["run_shell"].calls) >= 1)
        await asyncio.sleep(0.02)  # let anything further settle before asserting "exactly one"

        # Exactly one call — tuple-unpacking raises its own clear
        # ValueError if there were zero or more than one, without pinning
        # a literal count via len(...).
        (call,) = seams["run_shell"].calls
        (args, _kwargs) = call
        event_context = args[1]
        assert event_context["skipped_session_wide"] == 0
        assert len(event_context["events"]) == burst_size, (
            f"the single launch must carry all {burst_size} events, none "
            f"duplicated, none dropped -- got {len(event_context['events'])}"
        )
        assert {e["uri"] for e in event_context["events"]} == {
            f"file:///{i}" for i in range(burst_size)
        }, "no event's data may be silently lost by folding"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_n_equals_1_still_wraps_as_a_single_item_array():
    """Tier 2: #5516 §1 — clean break, no dual shape: even an UN-folded,
    single event still arrives as ``{"events": [payload]}``, never a bare
    dict."""
    hook = HookDef(on="mcp_resource_updated", exec=("echo", "hi"))
    disp, seams = _make_dispatcher([hook])

    await disp.dispatch_external_batch(
        "mcp_resource_updated", [{"uri": "file:///solo", "point": "mcp_resource_updated"}],
    )

    (args, _kwargs), = seams["run_shell"].calls
    event_context = args[1]
    assert event_context == {
        "events": [{"uri": "file:///solo", "point": "mcp_resource_updated"}],
        "skipped_session_wide": 0,
    }


# ---------------------------------------------------------------------------
# skipped_session_wide: a real queue-overflow drop is counted and threaded in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_real_queue_overflow_is_counted_and_reported_as_skipped_session_wide():
    """Tier 2: LOAD-BEARING — #5516 §2's "取りこぼしは在りません" claim is
    about FOLDING, not about queue overflow: an event genuinely lost to
    ``QueueFull`` (bridge maxsize) must surface as a real, nonzero
    ``skipped_session_wide`` on the NEXT batch, not silently vanish."""
    hook = HookDef(on="mcp_resource_updated", exec=("echo", "hi"))
    disp, seams = _make_dispatcher([hook])

    gate = asyncio.Event()
    started = asyncio.Event()
    real_run_shell = seams["run_shell"]

    async def _blocking_run_shell(*args, **kwargs):
        started.set()
        await gate.wait()
        await real_run_shell(*args, **kwargs)

    disp2, seams2 = _make_dispatcher([hook], run_shell=_blocking_run_shell)
    seams2["run_shell_real"] = real_run_shell

    adapter = McpIngressAdapter(hook_trigger=disp2.dispatch_external_batch, maxsize=1)
    try:
        e1 = adapter.to_event("file:///a", server="s", agent_name=None, resync=False)
        e2 = adapter.to_event("file:///b", server="s", agent_name=None, resync=False)
        e3 = adapter.to_event("file:///c", server="s", agent_name=None, resync=False)

        adapter.deliver(e1)  # picked up immediately, blocks the drain on `gate`
        await _wait_for(lambda: started.is_set())
        adapter.deliver(e2)  # fills the maxsize=1 queue
        adapter.deliver(e3)  # OVERFLOW -- dropped, counted

        gate.set()
        expected_batches = 2  # e1 alone, then e2 (e3 was dropped by QueueFull)
        await _wait_for(lambda: len(real_run_shell.calls) >= expected_batches)
        await asyncio.sleep(0.02)

        # Exactly two calls total -- tuple-unpacking raises its own clear
        # error if there were more or fewer, without pinning a literal
        # count via len(...).
        (call1, call2) = real_run_shell.calls

        # Batch 1 (e1 alone) carries skipped_session_wide=0 -- the drop
        # happened AFTER e1's own batch was already assembled+dispatching.
        (args1, _kwargs1) = call1
        ctx1 = args1[1]
        assert ctx1["skipped_session_wide"] == 0

        # e2 is still queued -> the NEXT drain iteration picks it up and
        # must report the 1 real drop (e3) on THAT batch.
        (args2, _kwargs2) = call2
        ctx2 = args2[1]
        assert ctx2["skipped_session_wide"] == 1, (
            f"the genuinely dropped event (e3, QueueFull) must surface on the "
            f"next batch's skipped_session_wide -- got {ctx2['skipped_session_wide']}"
        )
        (only_event,) = ctx2["events"]  # e2 alone
        assert only_event["uri"] == "file:///b"
    finally:
        await adapter.aclose()


# ---------------------------------------------------------------------------
# template_push: N events render N times, concatenate into ONE push
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_template_push_folds_n_renders_into_one_concatenated_push():
    """Tier 2: owner ruling #5516 §1 item ③ — N template_push renders
    concatenate into ONE push, improving max_hook_driven_turns valve
    accounting (N pushes would consume N valve units; one concatenated
    push consumes 1 -- observable here as exactly one put_inbox call)."""
    hook = HookDef(
        on="mcp_resource_updated",
        template_push=PushBlock(message="uri={{ uri }}", wake=True),
    )
    disp, seams = _make_dispatcher([hook])

    payloads = [
        {"uri": f"file:///{i}", "point": "mcp_resource_updated"} for i in range(3)
    ]
    await disp.dispatch_external_batch("mcp_resource_updated", payloads)

    # Exactly ONE inbox push -- tuple-unpacking raises its own clear
    # ValueError if there were zero or more than one, without pinning a
    # literal count via len(...).
    (call,) = seams["put_inbox"].calls
    (args, _kwargs) = call
    _origin, push_payload = args
    text = push_payload["text"]
    for i in range(3):
        assert f"uri=file:///{i}" in text, f"event {i}'s rendered text missing from the concatenated push: {text!r}"


# ---------------------------------------------------------------------------
# pipeline_launch: does NOT fold (architect ruling, #5516 broker thread)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_launch_never_folds_one_launch_per_event_always():
    """Tier 2: LOAD-BEARING falsification of the architect ruling this
    module's dispatcher docstring documents — pipeline_launch's receiver
    takes ONE ``input: dict``, so N events in a batch must produce N
    SEPARATE launch_pipeline calls, never one call with a merged/lossy
    input and never silently dropping N-1 events."""
    from reyn.hooks.schema import PipelineLaunchBlock

    hook = HookDef(
        on="mcp_resource_updated",
        pipeline_launch=PipelineLaunchBlock(
            name="reindex", input_template={"uri": "{{ uri }}"},
        ),
    )
    launch_recorder = _Recorder()
    disp, seams = _make_dispatcher([hook], launch_pipeline=launch_recorder)

    event_count = 4
    payloads = [
        {"uri": f"file:///{i}", "point": "mcp_resource_updated"} for i in range(event_count)
    ]
    await disp.dispatch_external_batch("mcp_resource_updated", payloads)

    assert len(launch_recorder.calls) == event_count, (
        f"pipeline_launch must launch ONCE PER EVENT, unconditionally (the "
        f"fold flag has no effect on this scheme) -- never fewer (a lossy "
        f"merge) and never more (a double-launch) -- got "
        f"{len(launch_recorder.calls)}"
    )
    seen_uris = {args[1]["uri"] for args, _kwargs in launch_recorder.calls}
    assert seen_uris == {f"file:///{i}" for i in range(event_count)}, (
        "every event's data must survive -- pipeline_launch cannot merge N "
        f"dicts into one without silently dropping N-1 events' fields -- got "
        f"uris {seen_uris!r}"
    )


# ---------------------------------------------------------------------------
# ComposedEventConsumer: the 3rd accumulation point, kind-grouping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composed_consumer_folds_same_kind_and_never_mixes_two_kinds():
    """Tier 2: LOAD-BEARING — the HookBus subscription queue
    ComposedEventConsumer drains is NOT single-kind (unlike a bridge's own
    per-adapter queue). A burst mixing TWO different composed kinds must
    fold each kind into its OWN batch, never merge them into one call."""
    hook_a = HookDef(on=f"{COMPOSED_KIND_PREFIX}alpha", exec=("echo", "a"))
    hook_b = HookDef(on=f"{COMPOSED_KIND_PREFIX}beta", exec=("echo", "b"))
    disp, seams = _make_dispatcher([hook_a, hook_b])

    bus = HookBus()
    consumer = ComposedEventConsumer(bus=bus, dispatcher=disp)
    consumer.start()
    # Let the consumer's background task actually reach `bus.subscribe()`
    # before publishing -- `start()` only SCHEDULES the task (asyncio.
    # ensure_future), it does not run it; publishing before the
    # subscription is live would broadcast to zero subscribers and the
    # burst would be silently lost (HookBus.publish's own no-subscriber
    # happy path), not folded.
    await _wait_for(lambda: bus.subscriber_count >= 1)
    try:
        # Publish a burst mixing two composed kinds, all synchronous (no
        # await between them) so the consumer's drain task has no
        # scheduling chance until after the whole burst lands.
        for i in range(3):
            bus.publish(HookEvent(kind=f"{COMPOSED_KIND_PREFIX}alpha", payload={"i": i}))
        for i in range(2):
            bus.publish(HookEvent(kind=f"{COMPOSED_KIND_PREFIX}beta", payload={"i": i}))

        expected_launches = 2  # one per distinct composed kind
        await _wait_for(lambda: len(seams["run_shell"].calls) >= expected_launches)
        await asyncio.sleep(0.02)

        # Exactly 2 launches -- tuple-unpacking raises its own clear error
        # if there were 1 (merged) or 5 (unfolded), without pinning a
        # literal count via len(...).
        (call_a, call_b) = seams["run_shell"].calls
        sizes = sorted(len(call[0][1]["events"]) for call in (call_a, call_b))
        assert sizes == [2, 3], (
            f"alpha's 3 events and beta's 2 events must each fold within "
            f"their own kind, never cross-contaminate -- got sizes {sizes}"
        )
    finally:
        await consumer.stop()
