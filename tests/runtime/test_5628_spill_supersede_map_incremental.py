"""Tier 2: #5628 — `RouterHistoryBuffer._spill_supersede_map` used to
re-scan the WHOLE history on every `build_history`/`decompose_history_
for_retry` call (O(n) per call, history append-only and monotonically
growing — owner's own real machine: 9M chars, thousands of turns,
#5621 D7). Fixed: built once, lazily, on first access, then updated
incrementally (O(1) per new spill record) at the ONE place a spill
record is ever created (`spill_turn_content`).

This file verifies the INCREMENTAL claim behaviorally, never via a
duration assertion (CLAUDE.md testing policy: no test writes a
duration, either direction) — wrapping the injected `history_fn`
collaborator (a constructor parameter, the same class of thing
`media_store`/`project_dir_fn` already are — not private internal
state) with a call-counting stand-in, the same established technique
this repo's own call-count tests (#5592) already use.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.chat_message import Spillability
from tests.runtime.test_5296_pr2_byte_reduction_same_turn_retry import (
    _make_spill_session,
    _push,
)


def _spill_records(session) -> "list":
    return [m for m in session.history if m.role == "spill_record"]


def test_second_spill_does_not_rescan_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5628 accept — after the map is warmed once (the first
    `build_history()` call), a SECOND real spill does not trigger a
    fresh full scan of history: the injected `history_fn` collaborator
    is called the SAME number of times whether or not a 2nd spill
    happens afterward, because the map's own 2nd entry is added
    incrementally, in `spill_turn_content`, never by re-deriving the
    whole map from a scan again."""
    session = _make_spill_session(tmp_path, monkeypatch)
    hb = session._loop_driver._history_buffer
    huge1 = "M" * 50_000
    huge2 = "N" * 50_000
    _push(session, "user", "look something up", spillability=Spillability.NEVER)
    _push(session, "tool", huge1, tool_call_id="tc1", name="tool")
    _push(session, "tool", huge2, tool_call_id="tc2", name="tool")
    _push(session, "assistant", "ok, done", spillability=Spillability.NEVER)

    # Warm the map (the ONE full scan this test allows).
    hb.build_history()

    real_history_fn = hb._history_fn
    calls = {"n": 0}

    def _counting_history_fn():
        calls["n"] += 1
        return real_history_fn()

    monkeypatch.setattr(hb, "_history_fn", _counting_history_fn)

    replacement1 = hb.spill_turn_content(huge1, chain_id="c1", tool="tool", seq=1)
    assert replacement1 is not None and replacement1 != huge1

    calls_after_first_spill = calls["n"]

    replacement2 = hb.spill_turn_content(huge2, chain_id="c1", tool="tool", seq=2)
    assert replacement2 is not None and replacement2 != huge2

    assert calls["n"] == calls_after_first_spill, (
        f"a 2nd spill must not trigger ANY additional history_fn() call "
        f"— the map is updated incrementally (O(1)), never re-derived "
        f"by a fresh scan — got {calls_after_first_spill} calls after "
        f"the 1st spill, {calls['n']} after the 2nd"
    )

    # Correctness: both entries are genuinely present afterward.
    built = hb.build_history()
    tool_turns = {t["tool_call_id"]: t["content"] for t in built if t.get("tool_call_id") in ("tc1", "tc2")}
    assert tool_turns["tc1"] == replacement1
    assert tool_turns["tc2"] == replacement2


def test_non_spill_record_role_with_matching_meta_does_not_supersede(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5628 deny — the map's own inclusion test is `role ==
    "spill_record"` FIRST, not merely "carries the right meta key". A
    turn on an ORDINARY role (e.g. the write-time cap's own real
    output, `router_loop.py`, a DIFFERENT code path from
    `spill_turn_content`) that happens to also carry
    `SPILL_TARGET_CONTENT_HASH_META_KEY` in its own meta (a shape only
    `spill_turn_content` itself ever constructs in production, but not
    load-bearing to enforce that here) must NOT be picked up by the
    map — witnessed via `build_history()`, the public surface the
    map's own entries are ever consulted through, never a private-state
    read of the map itself."""
    import hashlib

    from reyn.runtime.chat_message import SPILL_TARGET_CONTENT_HASH_META_KEY

    session = _make_spill_session(tmp_path, monkeypatch)
    hb = session._loop_driver._history_buffer

    target_text = "an ordinary tool result, never actually spilled"
    # The REAL content_hash of target_text — if role-filtering were NOT
    # applied, this lookalike entry's own meta would genuinely match and
    # supersede target_turn below, making this a non-vacuous check.
    real_hash = "sha256:" + hashlib.sha256(target_text.encode("utf-8")).hexdigest()
    _push(
        session, "tool", target_text,
        tool_call_id="tc-target", name="tool",
        spillability=Spillability.NEVER,
    )
    # A DIFFERENT, ordinary-role entry that happens to carry the SAME
    # meta key + value the map reads for tc-target's own content — must
    # never supersede it, because its own role is not "spill_record".
    _push(
        session, "tool", "unrelated content, wrong role for supersession",
        tool_call_id="tc-lookalike", name="tool",
        meta={SPILL_TARGET_CONTENT_HASH_META_KEY: real_hash},
    )

    built = hb.build_history()
    target_turn = next(t for t in built if t.get("tool_call_id") == "tc-target")
    assert target_turn["content"] == target_text, (
        "an ordinary-role turn carrying the map's own meta key must "
        "never supersede anything — the map's own inclusion test reads "
        "role == 'spill_record' first"
    )
    assert not _spill_records(session), (
        "sanity: no real role='spill_record' entry exists in this "
        "scenario — only an ordinary tool turn with a lookalike meta key"
    )
