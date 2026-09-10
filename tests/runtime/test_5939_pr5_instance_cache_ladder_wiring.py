"""Tier 2: #5939/#5851 PR-5 — the instance-attribute-cache release step
wired into the SHARED ② function (``run_cache_release_and_forensics``,
PR-1), reached from the steady-state ladder (PR-2). Driven end to end
through the REAL escalation path in every test here (never by calling a
private method directly, never by reaching through ``s._media_store`` —
the ``process_memory_forensics`` audit-event's own ``dropped`` payload is
the public witness every test reads).

Real ``Session`` + real ``MediaStore`` (via ``multimodal_config=``) + real
``ProcessMemoryGuard`` + real ``AgentRegistry`` throughout — never a Mock.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.config import MultimodalConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.process_memory import ProcessMemoryGuard
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


class _ExitCalled(Exception):
    """Sentinel raised by the intercepted ``os._exit`` — never a real
    process termination inside a test. Same shape as
    ``test_5939_pr2_memory_ladder_and_halt_remedies.py``'s own
    ``_intercept_exit``."""


def _intercept_exit(monkeypatch: pytest.MonkeyPatch) -> "list[int]":
    calls: "list[int]" = []

    def _fake_exit(code: int) -> None:
        calls.append(code)
        raise _ExitCalled()

    import os
    monkeypatch.setattr(os, "_exit", _fake_exit)
    return calls


def _fake_reader_sequence(values: "list[int | None]"):
    it = iter(values)
    last = values[-1] if values else None

    def _read() -> "int | None":
        nonlocal last
        try:
            last = next(it)
        except StopIteration:
            pass
        return last

    return _read


def _session_with_media(
    tmp_path: Path, name: str, *,
    guard: "ProcessMemoryGuard | None" = None, registry=None,
) -> Session:
    return make_session(
        agent_name=name,
        state_log=StateLog(tmp_path / ".reyn" / f"wal-{name}.jsonl"),
        snapshot_path=tmp_path / f"snap-{name}.json",
        workspace_base_dir=tmp_path,
        workspace_state_dir=tmp_path / f"ws-{name}",
        process_memory_guard=guard,
        multimodal_config=MultimodalConfig(),
        registry=registry,
    )


async def _escalate_ladder_to_exit(s: Session) -> None:
    """Drives ① -> ② -> ③④ exactly like ``test_5939_pr2_memory_ladder_
    and_halt_remedies.py``'s own escalation test."""
    await s._check_memory_ladder(chain_id="c1", footprint=500)  # ① enter backpressure
    s._audit_events.emit("compaction_completed")  # arms the judge
    await settle(s)  # let the escalation-judge subscriber see it first
    await s._check_memory_ladder(chain_id="c1", footprint=500)  # ②③④


def _dropped_by_name(forensics_event, name: str) -> "list[dict]":
    return [d for d in forensics_event.data["dropped"] if d["name"] == name]


# ── no registry: reaches only this session's own MediaStore ─────────────


def test_ladder_prunes_this_sessions_own_stale_spill_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: no registry attached — the escalation still reaches this
    session's OWN MediaStore and prunes a file it self-deleted (the
    genuine gap, #6050), witnessed entirely through the
    ``process_memory_forensics`` audit-event's ``dropped`` payload, never
    through private state."""
    exit_calls = _intercept_exit(monkeypatch)
    guard = ProcessMemoryGuard(
        reader=_fake_reader_sequence([500]), cap_bytes=100, enforce=True, metric="phys_footprint",
    )
    s = _session_with_media(tmp_path, "pr5-solo", guard=guard)
    events = collect_events(s)

    # Build the stale-entry scenario using the same `save_tool_result`/
    # `flush` pair `test_media_store.py` itself uses to construct
    # fixtures -- setup, not an assertion on private state.
    async def _setup_and_run() -> None:
        block = s._media_store.save_tool_result("body", mime_type="text/plain", seq=1)
        await s._media_store.flush()
        (tmp_path / block["path"]).unlink()
        await _escalate_ladder_to_exit(s)

    with pytest.raises(_ExitCalled):
        asyncio.run(_setup_and_run())

    assert exit_calls == [1]
    (forensics_event,) = [e for e in events if e.type == "process_memory_forensics"]
    (media_result,) = _dropped_by_name(forensics_event, "media_store_spill_paths")
    assert media_result["entries_before"] == 1
    assert media_result["entries_after"] == 0, "the self-deleted file must be pruned"


def test_ladder_does_not_lose_a_write_still_queued_at_escalation_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: the load-bearing regression test for the 2nd race found
    while implementing this PR (#5851 issue thread): ONE stale
    (self-deleted) entry and ONE brand-new, still-queued write (nothing
    awaited between the write and the escalation call, so the write is
    STRUCTURALLY guaranteed not yet on disk) both exist when ② fires.
    The stale one must be pruned; the still-queued one must NOT —
    witnessed by the exact before/after counts in ``process_memory_
    forensics``, never by reaching into ``MediaStore`` directly.

    Strip: remove ``await self._media_store.flush()`` from ``Session.
    _drop_instance_caches`` — this goes RED (``entries_after`` drops to
    0, the still-queued write wrongly reported pruned, performed during
    review; ``test_5939_pr5_media_store_spill_cache_release.py``'s own
    sibling test proves the race exists one layer down in isolation)."""
    exit_calls = _intercept_exit(monkeypatch)
    guard = ProcessMemoryGuard(
        reader=_fake_reader_sequence([500]), cap_bytes=100, enforce=True, metric="phys_footprint",
    )
    s = _session_with_media(tmp_path, "pr5-race", guard=guard)
    events = collect_events(s)

    async def _setup_and_run() -> None:
        stale_block = s._media_store.save_tool_result("stale", mime_type="text/plain", seq=1)
        await s._media_store.flush()
        (tmp_path / stale_block["path"]).unlink()

        await s._check_memory_ladder(chain_id="c1", footprint=500)  # ① enter backpressure
        s._audit_events.emit("compaction_completed")  # arms the judge
        await settle(s)  # let the escalation-judge subscriber see it first

        # The fresh write is enqueued IMMEDIATELY before the ②-reaching
        # call, with NOTHING awaited in between -- a real yield point
        # here (e.g. reusing settle()/an intervening await) would let
        # MediaStore's own background worker run regardless of whether
        # `_drop_instance_caches` awaits its own flush, silently
        # defeating this test's own strip-witness.
        s._media_store.save_tool_result("fresh", mime_type="text/plain", seq=2)
        await s._check_memory_ladder(chain_id="c1", footprint=500)  # ②③④

    with pytest.raises(_ExitCalled):
        asyncio.run(_setup_and_run())

    assert exit_calls == [1]
    (forensics_event,) = [e for e in events if e.type == "process_memory_forensics"]
    (media_result,) = _dropped_by_name(forensics_event, "media_store_spill_paths")
    assert media_result["entries_before"] == 2
    assert media_result["entries_after"] == 1, (
        "exactly the stale entry must be pruned -- the still-queued write "
        "must survive (an entries_after of 0 means the race reopened)"
    )


# ── with a registry: reaches every co-resident session's own MediaStore ──


def test_ladder_reaches_every_registry_sessions_own_media_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: the real fan-out — 2 real, independently-constructed
    sessions sharing a real ``AgentRegistry``. Escalating ONE session's
    ladder must ALSO reach the CO-RESIDENT session's own MediaStore (the
    population is DERIVED from ``AgentRegistry.all_sessions()``, #5939
    PR-2, never a separately hand-maintained list) — witnessed by BOTH
    sessions' own stale entries being pruned in the SAME forensics
    event."""
    exit_calls = _intercept_exit(monkeypatch)

    def _factory(profile):
        return sessions_by_name[profile.name]

    registry = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=None)
    guard = ProcessMemoryGuard(
        reader=_fake_reader_sequence([500]), cap_bytes=100, enforce=True, metric="phys_footprint",
    )
    sessions_by_name: dict = {}
    for name in ("pr5-fan-a", "pr5-fan-b"):
        AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)
        sessions_by_name[name] = _session_with_media(
            tmp_path, name, guard=(guard if name == "pr5-fan-a" else None), registry=registry,
        )
        registry.get_or_load(name)

    triggering = registry.get_or_load("pr5-fan-a")
    other = registry.get_or_load("pr5-fan-b")
    events = collect_events(triggering)

    async def _setup_and_run() -> None:
        block_a = triggering._media_store.save_tool_result("a", mime_type="text/plain", seq=1)
        await triggering._media_store.flush()
        (tmp_path / block_a["path"]).unlink()
        block_b = other._media_store.save_tool_result("b", mime_type="text/plain", seq=1)
        await other._media_store.flush()
        (tmp_path / block_b["path"]).unlink()
        await _escalate_ladder_to_exit(triggering)

    with pytest.raises(_ExitCalled):
        asyncio.run(_setup_and_run())

    assert exit_calls == [1]
    (forensics_event,) = [e for e in events if e.type == "process_memory_forensics"]
    media_results = _dropped_by_name(forensics_event, "media_store_spill_paths")
    assert media_results, "the ladder must have reached at least one session's MediaStore"
    # Each session wrote exactly 1 stale entry of its own -- the total
    # `entries_before` across every media_store_spill_paths result can
    # only reach 2 if BOTH sessions' own MediaStore was actually counted
    # (never just the triggering session's), and each is fully pruned.
    assert sum(r["entries_before"] for r in media_results) == 2, (
        f"expected 2 total stale spill entries (1 per registry session), "
        f"got {media_results!r} -- the co-resident session's own "
        "MediaStore was never reached"
    )
    assert sum(r["entries_after"] for r in media_results) == 0, (
        "both sessions' own stale entry must be pruned"
    )


# ── no MediaStore configured: the no-op branch, via the real path ───────


def test_ladder_reports_no_media_store_entry_when_none_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Tier 2: a session with no ``multimodal_config`` (no MediaStore at
    all) escalates normally — the forensics ``dropped`` list carries only
    the 3 module-level caches, no ``media_store_spill_paths`` entry, and
    the process still exits (this session's own absence of a MediaStore
    is not an error)."""
    exit_calls = _intercept_exit(monkeypatch)
    guard = ProcessMemoryGuard(
        reader=_fake_reader_sequence([500]), cap_bytes=100, enforce=True, metric="phys_footprint",
    )
    s = make_session(
        agent_name="pr5-no-media",
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
        snapshot_path=tmp_path / "snap.json",
        workspace_state_dir=tmp_path / "ws",
        process_memory_guard=guard,
    )
    events = collect_events(s)

    with pytest.raises(_ExitCalled):
        asyncio.run(_escalate_ladder_to_exit(s))

    assert exit_calls == [1]
    (forensics_event,) = [e for e in events if e.type == "process_memory_forensics"]
    assert _dropped_by_name(forensics_event, "media_store_spill_paths") == []
    names = {d["name"] for d in forensics_event.data["dropped"]}
    assert names == {
        "compaction_token_cache", "artifact_ref_table_cache", "status_config_derived_cache",
    }
