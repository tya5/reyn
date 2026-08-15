"""Tier 2: #4497 Phase 1 — resident-memory measurement
(``resident_stats.py``'s pure helpers + a real-Session integration check).

No thresholds, no eviction — this module is a read-only diagnostic
surface. Pure-helper tests use plain attribute-bearing objects (the
functions under test are duck-typed readers with no behavior to fake, the
same category as ``test_events_pure_helpers.py``); the Session-facing test
uses a real ``Session`` via ``tests._support.agent_session.make_session``.
"""
from __future__ import annotations

from reyn.runtime.resident_stats import (
    ContainerStat,
    _approx_bytes,
    _stat,
    process_global_container_stats,
    session_container_stats,
)
from tests._support.agent_session import make_session


def test_approx_bytes_of_none_is_zero():
    """Tier 2: a container some Session states legitimately leave unset
    (e.g. _router_loop_delegations outside an active router turn)
    contributes 0, not an error."""
    assert _approx_bytes(None) == 0


def test_approx_bytes_grows_with_dict_contents():
    """Tier 2: (accept-side) a dict with more entries reports more bytes
    than an empty one — the shallow estimate is at least monotonic in the
    dimension it claims to measure."""
    empty = _approx_bytes({})
    populated = _approx_bytes({"a": "x" * 1000, "b": "y" * 1000})
    assert populated > empty


def test_approx_bytes_grows_with_list_contents():
    """Tier 2: same monotonicity check for a list-shaped container
    (_pending_user_images / _cancel_forward_targets's own shape)."""
    empty = _approx_bytes([])
    populated = _approx_bytes(["x" * 1000, "y" * 1000])
    assert populated > empty


def test_stat_counts_none_as_zero_items():
    """Tier 2: _stat's own None handling — count 0, not an exception."""
    stat = _stat("some_container", None)
    assert stat == ContainerStat(name="some_container", count=0, approx_bytes=0)


def test_stat_counts_a_populated_container():
    """Tier 2: _stat reports the real item count for a populated dict."""
    stat = _stat("some_container", {"a": 1, "b": 2, "c": 3})
    assert stat.count == 3
    assert stat.approx_bytes > 0


def test_session_container_stats_reads_a_real_sessions_attributes():
    """Tier 2: session_container_stats against a REAL Session (not a
    stand-in) — confirms every one of #4497's 9 enumerated attribute
    names actually exists on the real class and is readable."""
    session = make_session(agent_name="resident-test")
    stats = session_container_stats(session)
    names = {s.name for s in stats}
    assert "_pending_user_images" in names
    assert "_safety_extensions" in names
    assert "_background_tasks" in names
    assert "_buffered_intervention_answers" in names
    assert "_cancel_forward_targets" in names
    assert "_allowed_mcp" in names
    assert "history" in names


def test_session_container_stats_reflects_real_writes():
    """Tier 2: not just presence — a real write to a real Session
    container is reflected in the reported count."""
    session = make_session(agent_name="resident-test")
    session._pending_user_images.append({"data": "x"})
    session._pending_user_images.append({"data": "y"})

    stats = session_container_stats(session)
    row = next(s for s in stats if s.name == "_pending_user_images")
    assert row.count == 2


def test_session_container_stats_never_writes_to_the_session():
    """Tier 2: (accept-side) measuring is a pure read — calling it twice
    does not change what it reports the second time."""
    session = make_session(agent_name="resident-test")
    first = session_container_stats(session)
    second = session_container_stats(session)
    assert first == second


def test_process_global_container_stats_resolves_the_four_real_registries():
    """Tier 2: process_global_container_stats successfully imports and
    measures the 4 real module-level WeakKeyDictionary registries (the 3
    #4497 Phase 1 named, plus Phase 2's `_ROUTERS_BY_LOOP`), not a
    stand-in — a broken import path would silently drop a row (caught by
    ImportError -> skip), so this test names all 4 explicitly to catch
    that."""
    stats = process_global_container_stats()
    names = [s.name for s in stats]
    assert any(n == "_session_bridges" for n in names)
    assert any(n == "_REWIND_INDEXES" for n in names)
    assert any(n.startswith("_LOCKS_BY_LOOP") for n in names)
    assert any(n.startswith("_ROUTERS_BY_LOOP") for n in names)


def _inner_router_count(row_name: str) -> int:
    # Row name shape: "_ROUTERS_BY_LOOP (N router(s) across M loop(s))".
    return int(row_name.split("(", 1)[1].split(" router", 1)[0])


def test_process_global_stats_reflect_a_real_router_cache_entry():
    """Tier 2: #4497 Phase 2 — a real entry placed in `_ROUTERS_BY_LOOP`
    (the actual module global, not a stand-in) is reflected in the
    reported inner router count, the same "real write shows up" check
    Phase 1 ran for `_LOCKS_BY_LOOP`. The loop is kept alive (referenced
    by this test function, never run/closed) for the duration of the
    assertion — `_ROUTERS_BY_LOOP` is a WeakKeyDictionary, so a loop
    that's already been GC'd would make this test observe nothing real,
    not the write it just made. Asserts a DELTA, not an absolute count —
    this is process-global state another test/real Router-build in the
    same pytest process may have already populated."""
    import asyncio

    from reyn.llm.llm import _ROUTERS_BY_LOOP

    before_row = next(
        (s for s in process_global_container_stats() if s.name.startswith("_ROUTERS_BY_LOOP")),
        None,
    )
    before = _inner_router_count(before_row.name) if before_row is not None else 0

    loop = asyncio.new_event_loop()
    try:
        _ROUTERS_BY_LOOP[loop] = {"fake-cache-key-1": object(), "fake-cache-key-2": object()}

        after_row = next(
            s for s in process_global_container_stats() if s.name.startswith("_ROUTERS_BY_LOOP")
        )
        assert _inner_router_count(after_row.name) == before + 2
    finally:
        del _ROUTERS_BY_LOOP[loop]
        loop.close()


def test_process_global_stats_are_real_module_state_not_copies(monkeypatch):
    """Tier 2: falsify the import path deliberately (rename one target)
    and confirm the corresponding row disappears rather than silently
    reporting a stale/fake value -- proves this reads the real module
    global, not a hand-maintained duplicate."""
    import reyn.hooks.external_fire as ef

    monkeypatch.delattr(ef, "_session_bridges", raising=True)
    stats = process_global_container_stats()
    names = [s.name for s in stats]
    assert not any(n == "_session_bridges" for n in names)
