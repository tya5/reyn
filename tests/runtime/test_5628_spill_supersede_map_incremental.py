"""Tier 2: #5628 — `RouterHistoryBuffer._spill_supersede_map` used to
re-scan the WHOLE history on every `build_history`/`decompose_history_
for_retry` call (O(n) per call, history append-only and monotonically
growing — owner's own real machine: 9M chars, thousands of turns,
#5621 D7). Fixed: a watermark-based incremental scan — remember how
many entries were scanned and the ``.seq`` of the entry that sat at
that boundary; each call scans only the genuinely unscanned tail
UNLESS the current history is shorter, or the remembered boundary
entry no longer carries that same seq (either signals the active
branch changed under us — a rewind or fork-switch — and forces a
full rebuild).

Lead-coder's own PR review (#5644) caught the real defect the FIRST
version of this fix had: a spill_record is appended AFTER the turn it
supersedes, so a rewind/fork whose cut lands BETWEEN the two makes the
record invisible while the ORIGINAL turn stays visible — a
never-invalidated append-only cache would keep superseding it with a
stale preview, diverging from the durable active branch (the SAME
drift class #5612 already retired for `_spill_overlay`). This file's
own deny test drives exactly that scenario.

Per that same review: the accept side never pins the OLD "history_fn()
call count" shape (CLAUDE.md: never pin algorithm-level behaviour —
scan count is algorithm-level) — it asserts on 2 public-surface
contracts instead ("append -> visible on the very next build_history()
call", "regress -> rebuilds and reflects the new, smaller
population"). Both tests drive a `RouterHistoryBuffer` constructed
DIRECTLY with a caller-owned, mutable `history_fn` (the same
constructor-injection shape `test_session_router_history_slicing.py`'s
own `test_watermark_follows_whichever_history_the_producer_returns`
already establishes) — never a `monkeypatch.setattr` on the buffer's
own private `_history_fn` attribute, and never a reach into
`session._loop_driver._history_buffer`.
"""
from __future__ import annotations

import hashlib

from reyn.config.chat import CompactionConfig
from reyn.runtime.chat_message import (
    SPILL_TARGET_CONTENT_HASH_META_KEY,
    ChatMessage,
    Spillability,
)
from reyn.runtime.services.router_history_buffer import (
    SPILL_RECORD_MESSAGE_ROLE,
    RouterHistoryBuffer,
)
from tests._support.session import now as _now


def _make_buffer(state: "dict[str, list]") -> RouterHistoryBuffer:
    return RouterHistoryBuffer(
        history_fn=lambda: state["history"],
        compaction=CompactionConfig(),
        compaction_controller=None,
        model_fn=lambda: "openai/gpt-4o",
        events=None,
        media_store=None,
        router_host=None,
        universal_wrappers_enabled=False,
        non_interactive=False,
    )


def _spill_pair(seq: int, target_text: str) -> "tuple[ChatMessage, ChatMessage]":
    """A real (target-turn, spill_record) pair — the record carries the
    target's own real content_hash, the SAME shape `spill_turn_content`
    itself constructs, so `_spill_supersede_map`'s own inclusion test is
    genuinely exercised (not a vacuous, unrelated hash)."""
    target = ChatMessage(
        role="tool", content=target_text, ts=_now(), seq=seq,
        tool_call_id=f"tc{seq}", name="tool",
        spillability=Spillability.NEVER,
    )
    content_hash = "sha256:" + hashlib.sha256(target_text.encode("utf-8")).hexdigest()
    record = ChatMessage(
        role=SPILL_RECORD_MESSAGE_ROLE, content=f"[offloaded preview of seq {seq}]",
        ts=_now(), seq=seq + 100,
        meta={SPILL_TARGET_CONTENT_HASH_META_KEY: content_hash},
        spillability=Spillability.NEVER,
    )
    return target, record


def test_append_is_visible_on_the_very_next_build_history() -> None:
    """Tier 2: #5628 accept — a spill_record appended to the history the
    injected `history_fn` returns is picked up by the buffer's very next
    `build_history()` call, whether or not the map was already warmed by
    an earlier call (never pinning HOW that happens — scan count is
    algorithm-level, CLAUDE.md)."""
    target1, record1 = _spill_pair(1, "M" * 50_000)
    state = {"history": [target1]}
    buf = _make_buffer(state)

    built_before = buf.build_history()
    assert built_before[0]["content"] == target1.content, (
        "sanity: before any spill_record exists, the original content shows"
    )

    state["history"] = [target1, record1]
    built_after = buf.build_history()
    assert built_after[0]["content"] == record1.content, (
        "the SAME buffer instance must reflect a newly-appended "
        "spill_record on its very next build_history() call"
    )


def test_regress_to_a_smaller_population_rebuilds_correctly() -> None:
    """Tier 2: #5628 accept sibling — when the injected `history_fn`
    later returns a SHORTER list than the one it returned on a prior
    call (a real, reachable shape whenever the active branch changes),
    the buffer's next `build_history()` correctly reflects the new,
    smaller population instead of projecting stale state left over from
    the longer one."""
    target1, record1 = _spill_pair(1, "M" * 50_000)
    target2, record2 = _spill_pair(2, "N" * 50_000)
    state = {"history": [target1, record1, target2, record2]}
    buf = _make_buffer(state)

    built_before = buf.build_history()
    contents_before = [m["content"] for m in built_before]
    assert record1.content in contents_before and record2.content in contents_before

    # Regress: branch-switch/rewind drops the 2nd pair entirely.
    state["history"] = [target1, record1]
    built_after = buf.build_history()
    contents_after = [m["content"] for m in built_after]
    assert contents_after == [record1.content], (
        f"a regression to a shorter population must be fully reflected — "
        f"got {contents_after!r}"
    )


def test_rewind_drift_record_gone_target_stays_shows_original_not_stale_preview() -> None:
    """Tier 2: #5628 deny — the exact rewind-drift bug lead-coder's own
    PR review caught: a spill_record sits chronologically AFTER the turn
    it supersedes, so a rewind/fork-switch cut landing BETWEEN the two
    makes the record invisible while the original target turn stays
    visible. Once the SAME buffer instance's own `history_fn` starts
    returning that population, `build_history()` must show the ORIGINAL
    text — never a preview left over from a cache that was never
    invalidated for this direction."""
    target1, record1 = _spill_pair(1, "M" * 50_000)
    state = {"history": [target1, record1]}
    buf = _make_buffer(state)

    built_before = buf.build_history()
    assert built_before[0]["content"] == record1.content, (
        "sanity: with both target and record present, the preview shows "
        "— establishing the map was genuinely warmed with this entry"
    )

    # Rewind: the cut lands between target1 (kept) and record1 (dropped) —
    # the record disappears from the active branch, the target does not.
    state["history"] = [target1]
    built_after = buf.build_history()
    assert built_after[0]["content"] == target1.content, (
        f"once the spill_record drops off the active branch while its "
        f"own target turn stays, build_history() must return the "
        f"ORIGINAL text — got {built_after[0]['content']!r}"
    )


def test_non_spill_record_role_with_matching_meta_does_not_supersede() -> None:
    """Tier 2: #5628 deny — the map's own inclusion test is `role ==
    "spill_record"` FIRST, not merely "carries the right meta key". A
    turn on an ORDINARY role that happens to also carry
    `SPILL_TARGET_CONTENT_HASH_META_KEY` in its own meta (a shape only
    `spill_turn_content` itself ever constructs in production, but not
    load-bearing to enforce that here) must NOT be picked up by the
    map."""
    target_text = "an ordinary tool result, never actually spilled"
    real_hash = "sha256:" + hashlib.sha256(target_text.encode("utf-8")).hexdigest()
    target = ChatMessage(
        role="tool", content=target_text, ts=_now(), seq=1,
        tool_call_id="tc-target", name="tool",
        spillability=Spillability.NEVER,
    )
    lookalike = ChatMessage(
        role="tool", content="unrelated content, wrong role for supersession",
        ts=_now(), seq=2, tool_call_id="tc-lookalike", name="tool",
        meta={SPILL_TARGET_CONTENT_HASH_META_KEY: real_hash},
    )
    state = {"history": [target, lookalike]}
    buf = _make_buffer(state)

    built = buf.build_history()
    target_turn = next(m for m in built if m.get("tool_call_id") == "tc-target")
    assert target_turn["content"] == target_text, (
        "an ordinary-role turn carrying the map's own meta key must "
        "never supersede anything — the map's own inclusion test reads "
        "role == 'spill_record' first"
    )
