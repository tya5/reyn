"""Tier 2: #5438 (architect ruling — "compute, don't store") — a spilled
entry whose backing file is confirmed missing gets its WHY derived fresh
at read time, never persisted as a new field or ledger. Read-only
consumers: never `str`, always the typed `LostReason`.

Prior state (architect's own real-execution findings, this same issue):
`LOST_REASON_META_KEY` had 2 write sites (both `never_persisted`) and 0
read sites — the reader-less field this PR closes. `lost_reason` itself
is unchanged as a WRITE-time signal (the write-time cap's own real
"offload refused" fact, #5364 §1.5); what's new is the READ-time
derivation for the (much more common) case where a ref WAS minted and
its file later disappeared.

2 operator-facing faces per architect's own text, both exercised here:
the reason named inline in the placeholder every reader already sees,
and one `offloaded_content_unavailable` audit-event per distinct ref per
read (deduped within one `build_history()` call, never a process-global
cache — a still-missing file is reported again on the NEXT read).

Real Session + real RouterHistoryBuffer, no fakes — mirrors
tests/runtime/test_5364_lost_content_resolution.py's own established
construction (a spilled ChatMessage appended directly, its own ref file
either present or genuinely absent on disk).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import (
    CONTENT_REF_META_KEY,
    LOST_REASON_META_KEY,
    SPILLED_META_KEY,
    ChatMessage,
    LostReason,
)
from tests._support.agent_session import make_session
from tests._support.events import collect_events


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_lost_session(tmp_path: Path):
    return make_session(
        agent_name="lost-reason-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        safety=SafetyConfig(loop=LoopConfig(), on_limit=OnLimitConfig(mode="unattended")),
    )


def _append_spilled(session, *, ref: str, extra_meta: "dict | None" = None) -> None:
    meta = {SPILLED_META_KEY: True, CONTENT_REF_META_KEY: ref}
    if extra_meta:
        meta.update(extra_meta)
    session._append_history(ChatMessage(
        role="tool",
        content=f'...[truncated: 500 chars total — full body: read_file(path="{ref}")]...',
        ts=_now(), tool_call_id="tc1", name="tool", meta=meta,
    ))


def test_a_missing_ref_with_no_recorded_reason_derives_gc_and_emits_the_event(
    tmp_path: Path,
) -> None:
    """Tier 2: accept — a spilled entry whose file is missing and whose
    own meta carries NO `LOST_REASON_META_KEY` (the common shape: a ref
    WAS minted, its file is gone) derives `gc` — eviction is reyn's only
    deleter of an already-persisted ref. Both faces witnessed: the
    placeholder names the reason, and exactly one audit-event fires."""
    session = _make_lost_session(tmp_path)
    events = collect_events(session)
    _append_spilled(session, ref="gone.txt")
    assert not (tmp_path / "gone.txt").exists(), "sanity: the ref file is genuinely absent"

    history = session._loop_driver._history_buffer.build_history()

    (tool_msg,) = [m for m in history if m.get("role") == "tool"]
    assert "(reason: gc)" in tool_msg["content"], (
        f"expected the derived gc reason in the placeholder — got "
        f"{tool_msg['content']!r}"
    )

    unavailable = [
        (e.data["ref"], e.data["reason"]) for e in events
        if e.type == "offloaded_content_unavailable"
    ]
    assert unavailable == [("gone.txt", "gc")], (
        f"expected exactly one offloaded_content_unavailable event naming "
        f"gone.txt/gc — got {unavailable!r}"
    )
    ref_sha256 = next(
        e.data["ref_sha256"] for e in events
        if e.type == "offloaded_content_unavailable"
    )
    assert ref_sha256, "ref_sha256 must be non-empty"


def test_a_missing_ref_recorded_never_persisted_is_never_reported_as_gc(
    tmp_path: Path,
) -> None:
    """Tier 2: deny — an entry whose meta ALREADY carries `LostReason.
    NEVER_PERSISTED` (the write-time cap's own real "offload refused"
    fact) must keep that reason at read time, never fall back to `gc` —
    the write-time record is stronger evidence than the read-time
    default and must win."""
    session = _make_lost_session(tmp_path)
    events = collect_events(session)
    _append_spilled(
        session, ref="never-written.txt",
        extra_meta={LOST_REASON_META_KEY: LostReason.NEVER_PERSISTED},
    )
    assert not (tmp_path / "never-written.txt").exists(), "sanity: still absent"

    history = session._loop_driver._history_buffer.build_history()

    (tool_msg,) = [m for m in history if m.get("role") == "tool"]
    assert "(reason: never_persisted)" in tool_msg["content"], (
        f"expected never_persisted, not a gc fallback — got {tool_msg['content']!r}"
    )
    assert "(reason: gc)" not in tool_msg["content"]

    unavailable = [
        e.data["reason"] for e in events if e.type == "offloaded_content_unavailable"
    ]
    assert unavailable == ["never_persisted"], (
        f"expected exactly one event naming never_persisted — got {unavailable!r}"
    )


def test_a_present_file_names_no_reason_and_emits_no_event(tmp_path: Path) -> None:
    """Tier 2: deny sibling — a spilled entry whose backing file DOES
    exist is unaffected by any of this: no reason text, no event."""
    session = _make_lost_session(tmp_path)
    events = collect_events(session)
    (tmp_path / "present.txt").write_text("the real body", encoding="utf-8")
    _append_spilled(session, ref="present.txt")

    history = session._loop_driver._history_buffer.build_history()

    (tool_msg,) = [m for m in history if m.get("role") == "tool"]
    assert "reason:" not in tool_msg["content"]
    assert not [e for e in events if e.type == "offloaded_content_unavailable"], (
        "a present file must never emit the unavailable event"
    )


def test_the_same_missing_ref_is_reported_once_per_read_not_once_per_turn_in_it(
    tmp_path: Path,
) -> None:
    """Tier 2: accept sibling — the SAME missing ref appearing in 2
    different history entries within ONE `build_history()` call emits
    the audit-event only ONCE (deduped per read, per architect's own
    text) — never once per turn that happens to reference it, and never
    silenced permanently either (a fresh `seen_lost_refs` set is built on
    every call, not a process-global cache — a later, separate read call
    reports it again, which this test does not need to re-drive to trust:
    it is what `build_history`'s own per-call set construction, read
    directly in this PR's diff, already guarantees for the next call)."""
    session = _make_lost_session(tmp_path)
    events = collect_events(session)
    _append_spilled(session, ref="shared-gone.txt")
    session._append_history(ChatMessage(
        role="tool",
        content='...[truncated: 500 chars total — full body: read_file(path="shared-gone.txt")]...',
        ts=_now(), tool_call_id="tc2", name="tool",
        meta={SPILLED_META_KEY: True, CONTENT_REF_META_KEY: "shared-gone.txt"},
    ))

    session._loop_driver._history_buffer.build_history()

    unavailable = [
        e.data["ref"] for e in events if e.type == "offloaded_content_unavailable"
    ]
    assert unavailable == ["shared-gone.txt"], (
        f"the same ref, twice in one read, must emit once — got {unavailable!r}"
    )
