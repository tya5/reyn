"""Tier 2: #5939 PR-2 — the host-condition OR (``ProcessMemoryGuard.
host_critical``) and its wiring into ``Session._check_memory_ladder``'s
own OR-condition (this session's own cap, OR the host's free swap at/
under a configured threshold).

Real ``ProcessMemoryGuard``s throughout, real injected readers (never a
Mock, matching every other reader seam in this module family). The
platform-selected readers themselves
(``make_host_swap_free_reader``/``_read_darwin_swap_free_bytes``/
``_read_linux_swap_free_bytes``) are exercised for real, shape-only
(never pinning the actual byte value — third-party OS state), mirroring
``test_5851a_process_memory_observe.py``'s own W6 pattern for the
footprint reader.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.process_memory import ProcessMemoryGuard, make_host_swap_free_reader
from tests._support.agent_session import make_session
from tests._support.events import collect_events


def _session(tmp_path: Path, *, guard: "ProcessMemoryGuard | None" = None):
    return make_session(
        agent_name="pr2-host-agent",
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
        snapshot_path=tmp_path / "snap.json",
        workspace_state_dir=tmp_path / "ws",
        process_memory_guard=guard,
    )


# ── the real, platform-selected reader (Tier 1 — third-party OS state) ──


def test_real_host_swap_reader_returns_a_real_measurement_or_none():
    """Tier 1: on a supported platform (darwin/linux), the REAL reader
    returns ``int >= 0`` — a live syscall/file read, no mock. On any
    other platform, ``None``. The exact byte VALUE is host OS state —
    not pinned; only the shape."""
    import sys

    reader = make_host_swap_free_reader()
    value = reader()
    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        assert value is None or (isinstance(value, int) and value >= 0)
    else:
        assert value is None


# ── ProcessMemoryGuard.host_critical() ───────────────────────────────────


def test_host_critical_is_inert_by_default():
    """Tier 2: ``host_swap_critical_bytes=None`` (the default) means
    ``host_critical()`` is always False, regardless of what the reader
    would say — matches every other stage (a)/(b) mechanism's own
    inert-by-default shape."""
    guard = ProcessMemoryGuard(host_swap_reader=lambda: 0)  # "critically low", if it mattered
    assert guard.host_critical() is False


def test_host_critical_fires_at_or_under_the_threshold():
    """Tier 2: accept — a reading AT or BELOW the configured threshold is
    critical; strictly above is not."""
    guard = ProcessMemoryGuard(host_swap_critical_bytes=1000, host_swap_reader=lambda: 1000)
    assert guard.host_critical() is True

    guard2 = ProcessMemoryGuard(host_swap_critical_bytes=1000, host_swap_reader=lambda: 1001)
    assert guard2.host_critical() is False


def test_host_critical_never_fabricates_when_the_reader_has_nothing():
    """Tier 2: a reader returning ``None`` (unsupported platform, or a
    transient read failure) must not be treated as "critical" just
    because a threshold is configured."""
    guard = ProcessMemoryGuard(host_swap_critical_bytes=1000, host_swap_reader=lambda: None)
    assert guard.host_critical() is False


# ── wired into the ladder's own OR-condition ─────────────────────────────


def test_ladder_enters_backpressure_on_host_critical_alone_under_own_cap(tmp_path: Path):
    """Tier 2: the OR itself — THIS session's own footprint is well
    UNDER its own cap, but the host's free swap is at the configured
    critical threshold. The ladder must still enter backpressure
    (owner's own reasoning: a host already swap-starved by something
    ELSE is in danger regardless of this one process's own size).

    Strip: change the ladder's own ``if not over_cap and not
    host_critical`` to ``if not over_cap`` (drop the host term) — this
    goes RED (no backpressure event, performed during review)."""
    guard = ProcessMemoryGuard(
        reader=lambda: 10,  # far under cap_bytes
        cap_bytes=1_000_000,
        enforce=True,
        metric="phys_footprint",
        host_swap_critical_bytes=1000,
        host_swap_reader=lambda: 500,  # under the critical threshold
    )
    s = _session(tmp_path, guard=guard)
    events = collect_events(s)

    asyncio.run(s._check_memory_ladder(chain_id=None, footprint=10))

    (entered,) = [e for e in events if e.type == "session_memory_backpressure"]
    assert entered.data["bytes"] == 10  # this session's own footprint, still under its own cap
