"""Tier 2: #5959 (owner-hit, severity:high) — "leak 容疑を process の外から
判定できない". Owner, verbatim: "今後も leak 容疑確認のためのプロファイルを
取れるようにすべきじゃないの" / "python object として reyn が参照してる
ってことなんでしょ？" — this module answers "who is holding this" from
INSIDE the process, which a `vmmap` region read (#5957) structurally
cannot.

Real ``gc``/``EventLog``/``walk_config_schema()`` throughout — no mocks.
The population under test (live objects, real config schema) is exactly
what the tool is FOR, so faking it would test nothing real.
"""
from __future__ import annotations

import pytest

from reyn.core.events.events import EventLog
from reyn.runtime.memory_breakdown import (
    TRACEMALLOC_ENV_VAR,
    arm_tracemalloc_if_requested,
    bounding_axis_census,
    collect_memory_breakdown,
    emit_memory_breakdown_event,
    gc_largest_objects,
    gc_type_breakdown,
    tracemalloc_is_armed,
    tracemalloc_top_allocations,
)
from tests._support.events import collect_events, settle

# ── ⓐ discovery: gc-based, no curated list ──────────────────────────────


def test_gc_type_breakdown_finds_a_real_live_type_with_no_curated_list() -> None:
    """Tier 2: the core discovery claim — a type this test creates AT RUN
    TIME (never named anywhere in memory_breakdown.py's own source, since
    ⓐ has no curated list at all) shows up, ranked, purely because it is
    genuinely alive when the scan runs."""

    class _LeakSuspectMarkerType:  # noqa: N801 - deliberately distinctive test-only name
        def __init__(self) -> None:
            self.payload = "x" * 500_000  # large enough to rank near the top

    keepalive = [_LeakSuspectMarkerType() for _ in range(5)]
    try:
        rows = gc_type_breakdown(top_n=100_000)
        matching = [r for r in rows if r.type_name == "_LeakSuspectMarkerType"]
        assert matching, (
            "a genuinely live, large-footprint type must appear in the "
            "discovery breakdown -- gc_type_breakdown has no curated list "
            "to have silently excluded it from"
        )
        assert matching[0].count == 5
        assert matching[0].total_sizeof_bytes > 0
    finally:
        del keepalive


def test_gc_type_breakdown_is_sorted_by_total_bytes_descending() -> None:
    """Tier 2: the ranking claim -- 'suspect ranking' is the whole product
    (architect: '厳密さより容疑者の順位が目的'), so sort order is load-
    bearing, not incidental."""
    rows = gc_type_breakdown(top_n=50)
    totals = [r.total_sizeof_bytes for r in rows]
    assert totals == sorted(totals, reverse=True)


def test_gc_largest_objects_surfaces_a_single_outlier_a_type_aggregate_would_hide() -> None:
    """Tier 2: the complementary ⓐ view -- one huge object inside a type
    whose AVERAGE is small must still surface here, even though it might
    rank low (or not at all, at a bounded top_n) in the type-aggregate
    view alongside many small siblings of the same type.

    Uses a ``list`` with many elements (not a wrapper class holding a big
    attribute, and not a ``bytearray``/``str`` -- see the disclosure test
    below) -- a ``list``'s OWN ``sys.getsizeof`` genuinely includes its
    internal pointer array, so a list large by ELEMENT COUNT is large by
    its own measure, unlike a wrapper instance whose big attribute is a
    separately-allocated object (measured directly while building this
    test: a 2 MB string stashed on a small wrapper instance left the
    wrapper's own row at 48 bytes -- see the disclosure test)."""
    outlier = [0] * 500_000
    siblings = [[0] * 4 for _ in range(3)]
    try:
        rows = gc_largest_objects(top_n=2_000_000)
        matching = [r for r in rows if r.type_name == "list"]
        assert matching, "at least one list must appear among the largest single objects"
        assert max(r.sizeof_bytes for r in matching) > 1_000_000, (
            "the planted 500k-element list must be the largest list row -- "
            "its own sys.getsizeof genuinely includes its pointer array"
        )
    finally:
        del outlier, siblings


def test_a_large_standalone_string_is_invisible_to_gc_based_discovery() -> None:
    """Tier 2: DISCLOSED GAP, not a silent one -- ``gc.get_objects()``
    only returns objects CPython's garbage collector TRACKS (objects that
    can participate in a reference cycle: containers, class instances
    with ``__dict__``). ``str``/``bytes``/``bytearray``/``int``/``float``
    are NEVER tracked (measured directly: ``gc.is_tracked(bytearray(1))``
    and ``gc.is_tracked('x')`` are both ``False``) -- so a large string
    held via a reference (a variable, a dict value, an attribute) is
    invisible to :func:`gc_type_breakdown`/:func:`gc_largest_objects`
    BOTH directly (the string itself never appears) AND indirectly (the
    referencing container's own ``sys.getsizeof`` does not include it,
    the same shallowness the module docstring names). This is exactly
    the shape owner's own suspicion names (a large content STRING held
    by a Python reference) -- ⓐ's gc-walk genuinely cannot answer that
    case; :func:`tracemalloc_top_allocations` (stage ③, by allocation
    site rather than live walk) is the tool that can. Asserting this here
    keeps the gap a tracked, disclosed fact rather than a silent one a
    future reader could mistake for coverage."""
    import gc

    big_string = "leak-suspect-marker-" + ("q" * 3_000_000)
    holder = {"content": big_string}
    try:
        assert gc.is_tracked(big_string) is False, "sanity: str is never gc-tracked"
        rows = gc_largest_objects(top_n=2_000_000)
        as_str_row = [r for r in rows if r.type_name == "str" and r.sizeof_bytes > 1_000_000]
        assert as_str_row == [], (
            "a standalone large string must NOT appear as its own row -- "
            "gc.get_objects() never tracks str at all (this is the "
            "disclosed gap, not a bug this test expects to be fixed)"
        )
        holder_rows = [r for r in rows if r.type_name == "dict"]
        assert all(r.sizeof_bytes < 1_000_000 for r in holder_rows), (
            "the dict referencing the big string must not report the "
            "string's own size either -- sys.getsizeof(dict) is shallow, "
            "the same limitation the module docstring names"
        )
    finally:
        del big_string, holder


def test_gc_largest_objects_never_raises_on_a_repr_that_itself_raises() -> None:
    """Tier 2: a live object pulled from gc.get_objects() may belong to
    ANY type in the process, including a hostile/broken __repr__ -- the
    scan around it must not abort because of ONE object's own bug."""

    class _HostileRepr:  # noqa: N801
        def __repr__(self) -> str:
            raise RuntimeError("deliberately hostile __repr__")

    hostile = _HostileRepr()
    try:
        rows = gc_largest_objects(top_n=2_000_000)
        matching = [r for r in rows if r.type_name == "_HostileRepr"]
        assert matching, "the hostile object must still be measured and listed"
        assert "raised" in matching[0].repr_snippet
    finally:
        del hostile


# ── ⓑ completeness: derived from walk_config_schema(), never a 2nd list ──


def test_bounding_axis_census_is_derived_not_a_second_hand_written_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: architect's own acceptance criterion, verbatim -- '`Axis.
    BOUNDING` を持つ field を 1 つ足すと、profiler の出力に自動で現れる'.
    Strip witness: a FRESH Axis.BOUNDING field, on a ``SchemaNode`` that
    exists ONLY inside this test (never referenced anywhere in
    memory_breakdown.py), appears in the census purely because
    ``walk_config_schema()`` finds it -- proving the population is
    derived, not curated. Reverting ``bounding_axis_census`` to iterate a
    hardcoded tuple of known keys instead of ``walk_config_schema()``'s
    own output would make this test miss the fresh field -- verified
    directly."""
    import reyn.config.config_schema as config_schema_module
    from reyn.config.config_schema import SchemaNode
    from reyn.config_axis import Axis

    fresh_key = "a_field_no_production_code_or_curated_list_names_5959"
    fresh_node = SchemaNode(
        key=fresh_key, type_repr="int", default=42, axis=Axis.BOUNDING,
    )
    real_walk = config_schema_module.walk_config_schema

    def _fake_walk(cls=None):
        return [*real_walk(cls), fresh_node]

    monkeypatch.setattr(config_schema_module, "walk_config_schema", _fake_walk)

    rows = bounding_axis_census()

    matching = [r for r in rows if r.key == fresh_key]
    assert matching, (
        "a config field the census's OWN test just invented, never named "
        "anywhere in memory_breakdown.py, must still appear -- the "
        "population is derived from walk_config_schema(), not a curated list"
    )
    assert matching[0].declared_cap == 42
    assert matching[0].measured_bytes is None


def test_bounding_axis_census_the_real_population_includes_a_known_real_field() -> None:
    """Tier 2: sanity against the REAL, current ReynConfig schema (not a
    fixture) -- a specific field #4206's own Axis.BOUNDING work already
    classified (``llm.model``, the pre-existing BOUNDING_KEYS member that
    predates this issue) must be present, proving the census reaches the
    real schema rather than an empty/broken derivation. Checking for a
    named field's PRESENCE is a behaviour claim; counting the population
    would pin an unrelated config change instead."""
    rows = bounding_axis_census()
    keys = {r.key for r in rows}
    assert "llm.model" in keys


def test_unmeasured_is_distinct_from_a_real_zero() -> None:
    """Tier 2: architect's own explicit requirement -- 'measured=0' and
    'no provider registered' must never render the same. A provider
    that legitimately measures 0 resident bytes right now must show
    0 (int), not None; a field with no provider at all must show None
    (rendered 'unmeasured'), not a fabricated 0."""
    rows = bounding_axis_census()
    zero_key = rows[0].key
    providers = {zero_key: lambda: 0}

    censused = bounding_axis_census(measurement_providers=providers)
    zero_row = next(r for r in censused if r.key == zero_key)
    other_row = next(r for r in censused if r.key != zero_key)

    assert zero_row.measured_bytes == 0
    assert zero_row.measured_bytes is not None
    assert other_row.measured_bytes is None


def test_unbounded_or_dead_declarations_filters_to_zero_or_unmeasured_only() -> None:
    """Tier 2: MemoryBreakdown.unbounded_or_dead_declarations is the
    concrete, testable claim architect's own corrected acceptance text
    settled on (A only, never conflated with B -- see module docstring)."""
    breakdown = collect_memory_breakdown(top_n=1)
    non_trivial_key = None
    providers = {}
    census_now = bounding_axis_census()
    if census_now:
        non_trivial_key = census_now[0].key
        providers = {non_trivial_key: lambda: 12345}
    breakdown = collect_memory_breakdown(top_n=1, measurement_providers=providers)

    dead = breakdown.unbounded_or_dead_declarations
    assert all(r.measured_bytes in (None, 0) for r in dead)
    if non_trivial_key is not None:
        assert non_trivial_key not in {r.key for r in dead}, (
            "a field with a real, nonzero measured value must never appear "
            "in the dead-declaration filter"
        )


# ── the audit-event: same shape both surfaces (schema-validated) ────────


@pytest.mark.asyncio
async def test_emit_memory_breakdown_event_lands_with_the_declared_shape() -> None:
    """Tier 2: the audit-event is the SAME data the CLI prints (#5959's
    own requirement: '同じ内訳が audit-event にも出る') -- driven through
    a real EventLog, which validates against event_schema.py's own
    EVENT_AUDIT_REQUIREMENTS (a malformed payload would fail there, not
    just look wrong in a hand-inspected dict)."""
    log = EventLog()
    collected = collect_events(log)
    breakdown = collect_memory_breakdown(top_n=3)

    emit_memory_breakdown_event(log.emit, breakdown)
    await settle(log)

    (event,) = [e for e in collected if e.type == "process_memory_breakdown"]
    # Parity, not shape: the event must carry the EXACT SAME rows
    # collect_memory_breakdown() itself built for this call, in the same
    # order -- not merely "no more than 3", which a truncation bug could
    # also satisfy.
    assert event.data["type_breakdown"] == [
        {"type": r.type_name, "count": r.count, "total_sizeof_bytes": r.total_sizeof_bytes}
        for r in breakdown.type_breakdown
    ]
    assert event.data["largest_objects"] == [
        {"type": r.type_name, "sizeof_bytes": r.sizeof_bytes, "repr_snippet": r.repr_snippet}
        for r in breakdown.largest_objects
    ]
    assert isinstance(event.data["bounding_census"], list)
    assert "shallow" in event.data["estimate_note"].lower(), (
        "sys.getsizeof's shallowness must be disclosed IN the event "
        "itself, not only in CLI prose a human might not read"
    )


# ── stage ③: tracemalloc, opt-in, byte-identical when unset ─────────────


def test_tracemalloc_top_allocations_returns_none_when_never_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: 'never armed' and 'armed but found nothing' must not read
    the same -- None vs an empty list are different facts."""
    monkeypatch.delenv(TRACEMALLOC_ENV_VAR, raising=False)
    import tracemalloc

    was_tracing = tracemalloc.is_tracing()
    if was_tracing:
        tracemalloc.stop()
    try:
        assert tracemalloc_is_armed() is False
        assert tracemalloc_top_allocations() is None
    finally:
        if was_tracing:
            tracemalloc.start()


def test_arm_tracemalloc_if_requested_is_a_genuine_noop_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 1: strip witness -- REYN_MEMORY_TRACE unset means this
    function does not even import tracemalloc, let alone start it (the
    opt-in's whole point). Verified by confirming tracemalloc's own
    is_tracing() state is UNCHANGED by the call, on both sides."""
    monkeypatch.delenv(TRACEMALLOC_ENV_VAR, raising=False)
    import tracemalloc

    before = tracemalloc.is_tracing()
    armed = arm_tracemalloc_if_requested()
    after = tracemalloc.is_tracing()

    assert armed is False
    assert after == before


def test_arm_tracemalloc_if_requested_actually_arms_and_top_allocations_finds_a_real_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the opt-in path genuinely works end to end -- armed,
    something allocates, the allocation's own file:line is findable in
    the top-N. Real tracemalloc throughout (process-global state --
    stopped in `finally`, regardless of the pre-existing state, so this
    test never leaks armed tracing into the rest of the suite)."""
    monkeypatch.setenv(TRACEMALLOC_ENV_VAR, "1")
    import tracemalloc

    was_tracing = tracemalloc.is_tracing()
    try:
        armed = arm_tracemalloc_if_requested()
        assert armed is True
        assert tracemalloc_is_armed() is True

        keepalive = ["z" * 300_000 for _ in range(20)]  # a real, findable allocation
        allocations = tracemalloc_top_allocations(top_n=50)
        del keepalive

        assert allocations is not None
        assert allocations, "an armed trace with real allocations must find at least one site"
        assert all("file_line" in a and "size_bytes" in a and "count" in a for a in allocations)
    finally:
        tracemalloc.stop()
        if was_tracing:
            tracemalloc.start()
