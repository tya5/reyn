"""Tests for reyn.hooks.composer — Hook-Event Redesign Phase 4b (proposal
0059 §5). Covers: per-op firing, QueuePolicy overflow, fail-visible
composer_dropped on eviction, load-time cycle-check, Sync non-re-entry, and
the source-degeneracy load-time reject (a1).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from reyn.hooks.bus import HookBus
from reyn.hooks.composer import (
    Composer,
    ComposerConfigError,
    ComposerDef,
    ComposerInput,
    ComposerOp,
    ComposerPolicy,
    InMemoryPendingStore,
    PendingRecord,
    QueuePolicy,
    check_no_cycles,
    load_composers,
)
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.event import HookEvent
from reyn.hooks.event_pattern import EventPattern
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef, PushBlock


def _input(kind: str, match: "dict | None" = None) -> ComposerInput:
    return ComposerInput(kind=kind, pattern=EventPattern(kind=kind, payload=match))


def _recorder():
    """A real recording callable (no MagicMock/patch, per testing policy) —
    used both as a fake ``emit_event`` P6 sink and a fake dispatcher seam."""
    calls: "list[tuple]" = []

    def record(*args, **kwargs):
        calls.append((args, kwargs))

    return record, calls


def _assert_no_event(sub) -> None:
    """Assert nothing has been broadcast to *sub* yet, via the PUBLIC
    ``get_nowait()`` surface (never the subscription's private queue)."""
    with pytest.raises(asyncio.QueueEmpty):
        sub.get_nowait()


# ---------------------------------------------------------------------------
# Tier 1: config parsing (contract)
# ---------------------------------------------------------------------------


def test_load_composers_parses_valid_config():
    """Tier 1: a well-formed composers: block parses into typed ComposerDefs
    with the documented defaults (capacity/overflow/ttl)."""
    raw = [
        {
            "name": "deploy_approved",
            "op": "all",
            "inputs": [
                {"kind": "builtin:external:mcp_resource_updated", "match": {"server": "github"}},
                {"kind": "mcp:approval-server:approved"},
            ],
            "policy": {"capacity": 5, "overflow": "reject", "ttl": "5m"},
            "emit": {"kind": "composed:deploy_approved"},
        }
    ]
    (d,) = load_composers(raw)
    assert d.name == "deploy_approved"
    assert d.op is ComposerOp.ALL
    first_input, second_input = d.inputs
    assert first_input.kind == "builtin:external:mcp_resource_updated"
    assert second_input.kind == "mcp:approval-server:approved"
    assert d.emit_kind == "composed:deploy_approved"
    assert d.policy.capacity == 5
    assert d.policy.overflow is QueuePolicy.REJECT
    assert d.policy.ttl_seconds == 300.0


@pytest.mark.parametrize(
    "bad_entry,expect_substr",
    [
        ({"name": "x", "op": "bogus", "inputs": [{"kind": "a"}], "emit": {"kind": "composed:x"}}, "op="),
        ({"name": "x", "op": "all", "inputs": [], "emit": {"kind": "composed:x"}}, "inputs"),
        ({"name": "x", "op": "all", "inputs": [{"kind": "a"}], "emit": {"kind": "bare_kind"}}, "composed:"),
        (
            {"name": "x", "op": "all", "inputs": [{"kind": "a", "source": "mcp:github"}],
             "emit": {"kind": "composed:x"}},
            "can never match",
        ),
        ({"name": "x", "op": "seq", "inputs": [{"kind": "a"}], "emit": {"kind": "composed:x"}}, "seq"),
        (
            {"name": "x", "op": "correlate_by", "inputs": [{"kind": "a"}], "emit": {"kind": "composed:x"}},
            "correlate_by",
        ),
        ({"name": "x", "op": "count", "inputs": [{"kind": "a"}], "emit": {"kind": "composed:x"}}, "count"),
    ],
)
def test_load_composers_rejects_bad_config(bad_entry, expect_substr):
    """Tier 1: structurally invalid composers: entries fail loud at load time
    with a decision-enabling message (op typo, empty inputs, non-namespaced
    emit kind, degenerate `source` (a1), seq/correlate_by/count missing
    required companion fields)."""
    with pytest.raises(ComposerConfigError, match=expect_substr):
        load_composers([bad_entry])


def test_load_composers_parses_deadline_config():
    """Tier 1: `deadline`'s standalone `on`/`matcher` + `until` YAML shape
    (issue #3166 — distinct from every other op's `inputs: [{kind, match}]`
    list) parses into a ComposerDef with a 1-element inputs tuple, an
    until_input, the top-level `ttl` folded into policy.ttl_seconds, and a
    default `composed:<name>` emit kind when `emit:` is omitted. Also
    asserts the load-time crash-non-durability UserWarning (CLAUDE.md: never
    ship a silent dead-man switch) is emitted."""
    raw = [
        {
            "name": "job_overdue",
            "op": "deadline",
            "on": "mcp_resource_updated",
            "matcher": {"uri": "orch://job/*/started"},
            "until": {"on": "mcp_resource_updated", "matcher": {"uri": "orch://job/*/done"}},
            "correlate_by": "job_id",
            "ttl": 1800,
        }
    ]
    with pytest.warns(UserWarning, match="crash-non-durable"):
        (d,) = load_composers(raw)
    assert d.op is ComposerOp.DEADLINE
    (arm_input,) = d.inputs  # deadline's `on` builds a single-element inputs tuple
    assert arm_input.kind == "mcp_resource_updated"
    assert d.until_input is not None
    assert d.until_input.kind == "mcp_resource_updated"
    assert d.correlate_by == "job_id"
    assert d.policy.ttl_seconds == 1800.0
    assert d.emit_kind == "composed:job_overdue"


def test_load_composers_deadline_requires_until():
    """Tier 1: op=deadline without an `until` block fails loud at load time
    (never a runtime surprise)."""
    raw = [{"name": "x", "op": "deadline", "on": "a"}]
    with pytest.raises(ComposerConfigError, match="until"):
        load_composers(raw)


def test_load_composers_rejects_cycle():
    """Tier 1: a composer feeding on another composer's composed kind, which
    in turn feeds back on the first, is a load-time fail-loud cycle (§5
    invariant #4 — DAG check, never a runtime check)."""
    raw = [
        {
            "name": "a", "op": "any", "inputs": [{"kind": "composed:b"}],
            "emit": {"kind": "composed:a"},
        },
        {
            "name": "b", "op": "any", "inputs": [{"kind": "composed:a"}],
            "emit": {"kind": "composed:b"},
        },
    ]
    with pytest.raises(ComposerConfigError, match="cycle"):
        load_composers(raw)


def test_check_no_cycles_rejects_self_feed():
    """Tier 1: a composer that takes its own composed kind as an input is a
    1-cycle, rejected at load time."""
    d = ComposerDef(
        name="loopy", op=ComposerOp.ANY,
        inputs=(_input("composed:loopy"),), emit_kind="composed:loopy",
    )
    with pytest.raises(ComposerConfigError, match="own composed kind"):
        check_no_cycles([d])


def test_pending_record_is_json_snapshot_friendly():
    """Tier 1: a PendingRecord's shape survives a JSON round-trip once its
    HookEvents are expanded via dataclasses.asdict and its set is sorted —
    the seam invariant #1 depends on (a future WalBackedPendingStore needs a
    serializable shape, not a rewrite)."""
    record = PendingRecord(
        events=[HookEvent(kind="builtin:external:file_changed", payload={"path": "/a"})],
        matched_inputs={0, 1},
        seq_pos=1,
    )
    snapshot = {
        "events": [dataclasses.asdict(e) for e in record.events],
        "matched_inputs": sorted(record.matched_inputs),
        "seq_pos": record.seq_pos,
        "created_at": record.created_at,
        "last_at": record.last_at,
    }
    round_tripped = json.loads(json.dumps(snapshot))
    assert round_tripped["seq_pos"] == 1
    assert round_tripped["events"][0]["payload"] == {"path": "/a"}


# ---------------------------------------------------------------------------
# Tier 2: per-op firing (OS invariant — the composition semantics themselves)
# ---------------------------------------------------------------------------


def test_op_all_fires_only_when_every_input_arrives():
    """Tier 2: `all` fires exactly once both distinct inputs have arrived,
    and not on either alone."""
    bus = HookBus()
    emit, calls = _recorder()
    d = ComposerDef(
        name="c", op=ComposerOp.ALL,
        inputs=(_input("builtin:external:file_changed"), _input("builtin:external:cron_fired")),
        emit_kind="composed:c",
    )
    composer = Composer(d, bus=bus, emit_event=emit)
    sub = bus.subscribe()

    composer.handle_event(HookEvent(kind="builtin:external:file_changed", payload={"path": "/a"}))
    _assert_no_event(sub)  # only one of two inputs so far — no fire
    composer.handle_event(HookEvent(kind="builtin:external:cron_fired", payload={"job_name": "j"}))
    fired = sub.get_nowait()
    assert fired.kind == "composed:c"
    assert any(c[0][0] == "composer_fired" for c in calls)


def test_op_any_fires_on_first_matching_input():
    """Tier 2: `any` fires immediately on the first matching input, stateless."""
    bus = HookBus()
    d = ComposerDef(
        name="c", op=ComposerOp.ANY,
        inputs=(_input("builtin:external:file_changed"), _input("builtin:external:cron_fired")),
        emit_kind="composed:c",
    )
    composer = Composer(d, bus=bus)
    sub = bus.subscribe()
    composer.handle_event(HookEvent(kind="builtin:external:cron_fired", payload={"job_name": "j"}))
    assert sub.get_nowait().kind == "composed:c"


def test_op_seq_requires_configured_order():
    """Tier 2: `seq` only fires once its inputs arrive in CONFIGURED order;
    the same events out of order never fire it."""
    bus = HookBus()
    d = ComposerDef(
        name="c", op=ComposerOp.SEQ,
        inputs=(_input("builtin:external:file_changed"), _input("builtin:external:cron_fired")),
        emit_kind="composed:c",
    )
    composer = Composer(d, bus=bus)
    sub = bus.subscribe()

    # Out of order: cron before file_changed — never advances past position 0.
    composer.handle_event(HookEvent(kind="builtin:external:cron_fired", payload={"job_name": "j"}))
    _assert_no_event(sub)
    composer.handle_event(HookEvent(kind="builtin:external:file_changed", payload={"path": "/a"}))
    _assert_no_event(sub)
    composer.handle_event(HookEvent(kind="builtin:external:cron_fired", payload={"job_name": "j"}))
    assert sub.get_nowait().kind == "composed:c"


def test_op_correlate_by_separates_independent_keys():
    """Tier 2: `correlate_by` groups by a payload field — two independent
    correlation keys complete independently, neither firing early on the
    other's partial input."""
    bus = HookBus()
    d = ComposerDef(
        name="c", op=ComposerOp.CORRELATE_BY,
        inputs=(_input("builtin:external:file_changed"), _input("builtin:external:cron_fired")),
        emit_kind="composed:c", correlate_by="request_id",
    )
    composer = Composer(d, bus=bus)
    sub = bus.subscribe()
    composer.handle_event(
        HookEvent(kind="builtin:external:file_changed", payload={"path": "/a", "request_id": "r1"})
    )
    composer.handle_event(
        HookEvent(kind="builtin:external:file_changed", payload={"path": "/b", "request_id": "r2"})
    )
    _assert_no_event(sub)  # both keys only 1/2 complete
    composer.handle_event(
        HookEvent(kind="builtin:external:cron_fired", payload={"job_name": "j", "request_id": "r1"})
    )
    fired = sub.get_nowait()
    assert fired.payload["correlation_key"] == "r1"
    _assert_no_event(sub)  # r2 still incomplete


def test_op_count_fires_at_threshold():
    """Tier 2: `count` fires once `threshold` matching events arrive."""
    bus = HookBus()
    d = ComposerDef(
        name="c", op=ComposerOp.COUNT,
        inputs=(_input("builtin:external:file_changed"),),
        emit_kind="composed:c", threshold=3,
    )
    composer = Composer(d, bus=bus)
    sub = bus.subscribe()
    for _ in range(2):
        composer.handle_event(HookEvent(kind="builtin:external:file_changed", payload={"path": "/a"}))
    _assert_no_event(sub)
    composer.handle_event(HookEvent(kind="builtin:external:file_changed", payload={"path": "/a"}))
    assert sub.get_nowait().kind == "composed:c"


def test_op_window_buffers_then_fires_on_sweep():
    """Tier 2: `window` buffers matching events and fires the whole buffer
    once `ttl` has elapsed since the FIRST one — driven deterministically via
    `sweep(now=...)`, no real-time sleeping."""
    bus = HookBus()
    d = ComposerDef(
        name="c", op=ComposerOp.WINDOW,
        inputs=(_input("builtin:external:file_changed"),),
        emit_kind="composed:c", policy=ComposerPolicy(ttl_seconds=10.0),
    )
    store = InMemoryPendingStore()
    composer = Composer(d, bus=bus, pending_store=store)
    sub = bus.subscribe()
    t0 = 1000.0
    composer.handle_event(HookEvent(kind="builtin:external:file_changed", payload={"path": "/a"}))
    store.get("c", "__default__").created_at = t0
    composer.sweep(now=t0 + 5)  # inside the window — no fire yet
    _assert_no_event(sub)
    composer.sweep(now=t0 + 11)  # window elapsed — fires
    fired = sub.get_nowait()
    (only,) = fired.payload["inputs"]
    assert only["path"] == "/a"


def test_op_debounce_fires_after_quiet_period():
    """Tier 2: `debounce` fires `ttl` seconds after the LAST matching event,
    not the first — a new event resets the quiet-period clock."""
    bus = HookBus()
    d = ComposerDef(
        name="c", op=ComposerOp.DEBOUNCE,
        inputs=(_input("builtin:external:file_changed"),),
        emit_kind="composed:c", policy=ComposerPolicy(ttl_seconds=10.0),
    )
    store = InMemoryPendingStore()
    composer = Composer(d, bus=bus, pending_store=store)
    sub = bus.subscribe()
    t0 = 1000.0
    composer.handle_event(HookEvent(kind="builtin:external:file_changed", payload={"path": "/a"}))
    store.get("c", "__default__").last_at = t0
    composer.sweep(now=t0 + 5)
    _assert_no_event(sub)
    composer.handle_event(HookEvent(kind="builtin:external:file_changed", payload={"path": "/b"}))
    store.get("c", "__default__").last_at = t0 + 5
    composer.sweep(now=t0 + 10)  # only 5s since the reset last_at — still quiet-period
    _assert_no_event(sub)
    composer.sweep(now=t0 + 16)
    fired = sub.get_nowait()
    assert fired.payload["inputs"][-1]["path"] == "/b"


def test_op_deadline_fires_when_until_never_arrives():
    """Tier 2: `deadline`'s reversal of the sweep (issue #3166) — the pending
    record FIRES (not discards) when `until` never arrives within ttl. The
    payload carries armed_at/ttl/awaited so an operator can see WHY it fired
    (firing here means something did NOT happen, not an error)."""
    bus = HookBus()
    d = ComposerDef(
        name="job_overdue", op=ComposerOp.DEADLINE,
        inputs=(_input("orch:job_started"),),
        until_input=_input("orch:job_done"),
        emit_kind="composed:job_overdue",
        policy=ComposerPolicy(ttl_seconds=10.0),
    )
    store = InMemoryPendingStore()
    composer = Composer(d, bus=bus, pending_store=store)
    sub = bus.subscribe()
    t0 = 1000.0
    composer.handle_event(HookEvent(kind="orch:job_started", payload={"job_id": "j1"}))
    store.get("job_overdue", "__default__").created_at = t0
    composer.sweep(now=t0 + 5)  # inside the deadline — no fire yet
    _assert_no_event(sub)
    composer.sweep(now=t0 + 11)  # ttl elapsed, until never arrived — fires
    fired = sub.get_nowait()
    assert fired.kind == "composed:job_overdue"
    assert fired.payload["armed_at"] == t0
    assert fired.payload["ttl"] == 10.0
    assert fired.payload["awaited"]["kind"] == "orch:job_done"
    assert store.get("job_overdue", "__default__") is None  # pending consumed, not left dangling


def test_op_deadline_does_not_fire_when_disarmed_in_time():
    """Tier 2: (★★ negative witness — the load-bearing one) `until` arriving
    WITHIN ttl disarms the key and it does NOT fire. An always-fires
    implementation would pass every other deadline test in this file, so
    this is the test that actually distinguishes `deadline` from a bare
    ttl-eviction relabel."""
    bus = HookBus()
    d = ComposerDef(
        name="job_overdue", op=ComposerOp.DEADLINE,
        inputs=(_input("orch:job_started"),),
        until_input=_input("orch:job_done"),
        emit_kind="composed:job_overdue",
        policy=ComposerPolicy(ttl_seconds=10.0),
    )
    store = InMemoryPendingStore()
    composer = Composer(d, bus=bus, pending_store=store)
    sub = bus.subscribe()
    t0 = 1000.0
    composer.handle_event(HookEvent(kind="orch:job_started", payload={"job_id": "j1"}))
    store.get("job_overdue", "__default__").created_at = t0
    composer.handle_event(HookEvent(kind="orch:job_done", payload={"job_id": "j1"}))
    assert store.get("job_overdue", "__default__") is None  # disarmed immediately
    composer.sweep(now=t0 + 11)  # well past ttl — but already disarmed
    _assert_no_event(sub)


def test_op_deadline_key_separation_only_undisarmed_key_fires():
    """Tier 2: two independent `correlate_by=job_id` keys — disarming only
    j1 means only j2 (never disarmed) fires at ttl; j1 stays silent."""
    bus = HookBus()
    d = ComposerDef(
        name="job_overdue", op=ComposerOp.DEADLINE,
        inputs=(_input("orch:job_started"),),
        until_input=_input("orch:job_done"),
        emit_kind="composed:job_overdue", correlate_by="job_id",
        policy=ComposerPolicy(ttl_seconds=10.0),
    )
    store = InMemoryPendingStore()
    composer = Composer(d, bus=bus, pending_store=store)
    sub = bus.subscribe()
    t0 = 1000.0
    composer.handle_event(HookEvent(kind="orch:job_started", payload={"job_id": "j1"}))
    composer.handle_event(HookEvent(kind="orch:job_started", payload={"job_id": "j2"}))
    store.get("job_overdue", "j1").created_at = t0
    store.get("job_overdue", "j2").created_at = t0
    composer.handle_event(HookEvent(kind="orch:job_done", payload={"job_id": "j1"}))  # only j1 disarmed
    composer.sweep(now=t0 + 11)
    fired = sub.get_nowait()
    assert fired.payload["correlation_key"] == "j2"
    _assert_no_event(sub)  # j1 never fires — nothing further on the bus


def test_op_deadline_armed_state_is_lost_on_crash_v1_pin():
    """Tier 2: (crash witness) pins v1's InMemoryPendingStore posture — a
    'crash' (process restart => fresh store, the only PendingStore v1 has)
    silently drops an armed deadline's state. This is the documented,
    owner-ratified v1 behavior (module docstring invariant #1, proposal §5
    Q-reyn-1: 'best-effort now, WAL-backed later'), not a bug — this test
    records the fact as an observed behavior so a future WAL-backed
    PendingStore's arrival is a visible, deliberate behavior change."""
    bus = HookBus()
    d = ComposerDef(
        name="job_overdue", op=ComposerOp.DEADLINE,
        inputs=(_input("orch:job_started"),),
        until_input=_input("orch:job_done"),
        emit_kind="composed:job_overdue",
        policy=ComposerPolicy(ttl_seconds=10.0),
    )
    store = InMemoryPendingStore()
    composer = Composer(d, bus=bus, pending_store=store)
    t0 = 1000.0
    composer.handle_event(HookEvent(kind="orch:job_started", payload={"job_id": "j1"}))
    store.get("job_overdue", "__default__").created_at = t0
    # Simulate a process crash: a fresh Composer over a fresh
    # InMemoryPendingStore — the armed state above does NOT carry over.
    crashed_store = InMemoryPendingStore()
    recovered = Composer(d, bus=bus, pending_store=crashed_store)
    sub = bus.subscribe()
    recovered.sweep(now=t0 + 11)
    _assert_no_event(sub)  # the dead-man monitor never fires — its armed state is gone
    assert crashed_store.keys("job_overdue") == []


# ---------------------------------------------------------------------------
# Tier 2: QueuePolicy overflow + fail-visible composer_dropped
# ---------------------------------------------------------------------------


def test_capacity_overflow_drop_oldest_evicts_and_is_fail_visible():
    """Tier 2: capacity overflow with DropOldest evicts the oldest pending
    key and emits composer_dropped (metadata only, no payload content) —
    the eviction is never silent (§5 invariant #3)."""
    bus = HookBus()
    emit, calls = _recorder()
    d = ComposerDef(
        name="c", op=ComposerOp.CORRELATE_BY,
        inputs=(_input("builtin:external:file_changed"), _input("builtin:external:cron_fired")),
        emit_kind="composed:c", correlate_by="request_id",
        policy=ComposerPolicy(capacity=1, overflow=QueuePolicy.DROP_OLDEST, ttl_seconds=300),
    )
    composer = Composer(d, bus=bus, emit_event=emit)
    composer.handle_event(
        HookEvent(kind="builtin:external:file_changed", payload={"path": "/a", "request_id": "r1"})
    )
    # capacity is 1 — a second, distinct key evicts the first (r1).
    composer.handle_event(
        HookEvent(kind="builtin:external:file_changed", payload={"path": "/b", "request_id": "r2"})
    )
    dropped = [c for c in calls if c[0][0] == "composer_dropped"]
    (only_drop,) = dropped
    _, kwargs = only_drop
    assert kwargs["correlation_key"] == "r1"
    assert kwargs["reason"] == "capacity_drop_oldest"
    assert "payload" not in kwargs and "path" not in kwargs  # metadata only, never content


def test_ttl_evict_is_fail_visible():
    """Tier 2: an incomplete pending correlation that ages past ttl is
    evicted and ALWAYS emits composer_dropped (reason=ttl_evict) — silent
    drop is never allowed (§5 invariant #3)."""
    bus = HookBus()
    emit, calls = _recorder()
    d = ComposerDef(
        name="c", op=ComposerOp.ALL,
        inputs=(_input("builtin:external:file_changed"), _input("builtin:external:cron_fired")),
        emit_kind="composed:c", policy=ComposerPolicy(ttl_seconds=10.0),
    )
    store = InMemoryPendingStore()
    composer = Composer(d, bus=bus, emit_event=emit, pending_store=store)
    composer.handle_event(HookEvent(kind="builtin:external:file_changed", payload={"path": "/a"}))
    record = store.get("c", "__default__")
    record.created_at = 1000.0
    composer.sweep(now=1011.0)
    dropped = [c for c in calls if c[0][0] == "composer_dropped"]
    (only_drop,) = dropped
    assert only_drop[1]["reason"] == "ttl_evict"
    assert store.get("c", "__default__") is None  # actually evicted, not just logged


def test_per_key_event_storm_is_bounded_and_fail_visible():
    """Tier 2: (#2890 F7) a single correlation key hammered by an external-
    event storm (a WINDOW composer buffering every matching event until its
    ttl closes) does NOT grow that key's events list unboundedly —
    ``policy.max_events_per_key`` caps it (drop-oldest), and every overflow
    fires a fail-visible ``composer_dropped`` (reason=``per_key_event_cap``,
    metadata only — never the dropped event's payload).

    Strip-falsify: remove the ``while len(record.events) > cap: ...`` loop in
    ``Composer._append_event_bounded`` (``reyn/hooks/composer.py``) and the
    stored record's events list grows to ``storm_size`` instead of staying
    capped, and zero ``composer_dropped`` calls fire — this test goes RED.
    """
    bus = HookBus()
    emit, calls = _recorder()
    cap = 5
    d = ComposerDef(
        name="c", op=ComposerOp.WINDOW,
        inputs=(_input("builtin:external:file_changed"),),
        emit_kind="composed:c",
        policy=ComposerPolicy(capacity=1, ttl_seconds=300, max_events_per_key=cap),
    )
    store = InMemoryPendingStore()
    composer = Composer(d, bus=bus, emit_event=emit, pending_store=store)

    storm_size = 12
    for i in range(storm_size):
        composer.handle_event(
            HookEvent(kind="builtin:external:file_changed", payload={"n": i})
        )

    record = store.get("c", "__default__")
    assert len(record.events) == cap  # bounded, not storm_size
    # the SURVIVING events are the newest (drop-oldest within the key)
    assert [e.payload["n"] for e in record.events] == list(range(storm_size - cap, storm_size))

    dropped = [c for c in calls if c[0][0] == "composer_dropped"]
    assert len(dropped) == storm_size - cap
    assert all(kw["reason"] == "per_key_event_cap" for (_a, kw) in dropped)
    assert all("payload" not in kw and "n" not in kw for (_a, kw) in dropped)


def test_count_threshold_above_max_events_per_key_is_rejected_at_load_time():
    """Tier 2: (#2890 F7) ``count`` reads ``len(record.events)`` off the SAME
    list ``max_events_per_key`` caps — a threshold above the cap could never
    fire. Caught fail-loud at config-load time (``ComposerConfigError``),
    not a silent runtime never-fires footgun."""
    with pytest.raises(ComposerConfigError, match="max_events_per_key"):
        load_composers([{
            "name": "c",
            "op": "count",
            "inputs": [{"kind": "builtin:external:file_changed"}],
            "emit": {"kind": "composed:c"},
            "count": 20,
            "policy": {"max_events_per_key": 5},
        }])


# ---------------------------------------------------------------------------
# Tier 2: Sync non-re-entry (§5 invariant #5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composed_event_never_triggers_sync_hooks_for():
    """Tier 2: a composed event is published to the Bus only — it is NEVER
    routed through HookDispatcher.dispatch()/HookRegistry.hooks_for(), so a
    Sync hook registered on the composed kind (a hypothetical `composed:*`
    hooks: entry) never fires from Composer activity alone. This is the OS
    invariant behind the "composition graph never re-enters Sync dispatch"
    rule — bounded reactivity via max_hook_driven_turns stays intact."""
    bus = HookBus()
    # A Sync hook registered on the COMPOSED kind — if the Composer ever
    # looped composed events back into Sync dispatch, this would fire.
    hook = HookDef(on="composed:c", template_push=PushBlock(message="should never fire"))
    registry = HookRegistry([hook])
    put_inbox, inbox_calls = _recorder()
    stage, stage_calls = _recorder()

    async def _put_inbox(kind, payload):
        put_inbox(kind, payload)

    async def _stage(kind, payload):
        stage(kind, payload)

    dispatcher = HookDispatcher(registry, put_inbox=_put_inbox, stage_next_turn_context=_stage, bus=bus)

    d = ComposerDef(
        name="c", op=ComposerOp.ANY, inputs=(_input("builtin:external:file_changed"),),
        emit_kind="composed:c",
    )
    composer = Composer(d, bus=bus)
    sub = bus.subscribe()

    # Drive the underlying event through the REAL dispatcher (as production
    # code does) — this both runs Sync dispatch for file_changed AND
    # broadcasts to the bus, which the Composer observes.
    await dispatcher.dispatch("file_changed", {"path": "/a", "event_type": "modified"})
    composer.handle_event(sub.get_nowait())  # the file_changed broadcast reaches the composer

    composed = sub.get_nowait()
    assert composed.kind == "composed:c"
    # The composed event was published straight to the bus — never run through
    # dispatcher.dispatch()/hooks_for(), so the Sync hook on composed:c never fired.
    assert inbox_calls == []
    assert stage_calls == []
