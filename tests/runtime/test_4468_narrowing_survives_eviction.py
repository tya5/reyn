"""Tier 2: #4468 (lead-coder security review of #4387) — capability
narrowing must not silently lift when the untrusted-content entry that
triggered it is evicted from ``Session.history``'s resident set by #4387's
byte cap, while that entry is still logically ACTIVE (its ``seq`` is above
the compaction watermark — narrowing has NOT genuinely cleared, only
memory has).

``_ephemeral_contextual_for_turn``'s taint scan only ever reads RESIDENT
entries (``self.history``), so #4387's byte-driven eviction — unlike
compaction, which is the ONLY mechanism meant to retire an untrusted
entry's taint — can silently drop the taint signal for an entry compaction
has not yet folded away. This is exactly CLAUDE.md's named shape ("removing
one layer regrants a denied capability" — #3916's falsification family).

Fixed via ``Session._max_evicted_untrusted_seq``, a monotone OR-latch
(mirrors #4381 PR-2's in-flight latch) set by
``_evict_oldest_resident_entries`` whenever it evicts an entry carrying the
untrusted marker, ORed into the scan alongside the resident scan and the
in-flight latch. Self-clears the identical way the resident scan does —
once REAL compaction advances the watermark past the latched seq (not
merely once the entry leaves memory).

Real ``Session`` + real ``ChatMessage`` + real narrowing config throughout
— same seam ``tests/runtime/test_3380_tool_tab_ephemeral_narrowing.py``'s
own compaction-watermark test (``test_narrowing_self_clears_when_a_real_
compaction_covers_the_taint``) already established for the sibling
(compaction-side) clearing behaviour.
"""
from __future__ import annotations

from pathlib import Path

from reyn.config.chat import HistoryResidentConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.untrusted_narrowing import narrowing_on


def _session(tmp_path: Path, *, max_bytes: int) -> Session:
    return make_session(
        agent_name="narrowing-evict-test",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        safety=narrowing_on(),
        history_resident_config=HistoryResidentConfig(max_bytes=max_bytes),
    )


def _narrowed(s: Session) -> bool:
    """True iff the untrusted-content narrowing is currently engaged —
    read via the same public term the live gate composes
    (``_ephemeral_contextual_for_turn``), not a re-derived proxy."""
    return s._ephemeral_contextual_for_turn() is not None


def test_narrowing_survives_eviction_of_the_tainted_entry_while_still_active(
    tmp_path,
):
    """Tier 2: the exact regression this dispatch blocked on. A tiny byte
    cap evicts the tainted entry from memory well before compaction would
    ever fold it away — narrowing must STILL engage."""
    s = _session(tmp_path, max_bytes=300)  # small enough that padding
    # turns below will genuinely evict the tainted entry from memory.

    s._append_history(
        ChatMessage(
            role="user", content="<<<EXTERNAL>>> untrusted payload",
            meta={"external_source": True},
        )
    )
    tainted_seq = s.history[-1].seq
    assert _narrowed(s), "control arm: the taint must engage narrowing before eviction"

    # Pad with plain turns until the tainted entry is genuinely evicted from
    # the resident set (never compacted -- no summary entry appended here).
    for i in range(30):
        s._append_history(ChatMessage(role="user", content=f"padding turn {i} " + "x" * 20))

    resident_seqs = [m.seq for m in s.history]
    assert tainted_seq not in resident_seqs, (
        "sanity: the tainted entry must have been genuinely EVICTED (not just "
        "still resident) for this test to exercise the eviction-side latch "
        "at all -- if this fails, tighten max_bytes or add more padding"
    )

    assert _narrowed(s), (
        "narrowing silently lifted after the tainted entry left memory, "
        "while it was still ACTIVE (not yet compacted) -- eviction must "
        "never regrant a capability compaction alone is meant to release"
    )


def test_narrowing_still_self_clears_once_real_compaction_covers_the_evicted_entry(
    tmp_path,
):
    """Tier 2: accept-side — the eviction-side latch must not become a
    permanent, un-clearable narrowing. Once a REAL compaction watermark
    (a role="summary" entry with covers_through_seq at/above the evicted
    entry's own seq) advances past the latched seq, narrowing must clear,
    exactly as it already does for the resident-scan case."""
    s = _session(tmp_path, max_bytes=300)

    s._append_history(
        ChatMessage(
            role="user", content="<<<EXTERNAL>>> untrusted payload",
            meta={"external_source": True},
        )
    )
    tainted_seq = s.history[-1].seq

    for i in range(30):
        s._append_history(ChatMessage(role="user", content=f"padding turn {i} " + "x" * 20))

    assert tainted_seq not in [m.seq for m in s.history], (
        "sanity: the tainted entry must have been evicted for this test to "
        "exercise the eviction-latch's OWN clearing behaviour"
    )
    assert _narrowed(s), "sanity: still narrowed via the eviction-side latch"

    # A real compaction covering the tainted entry's seq -- the exact shape
    # CompactionController.make_summary_message produces.
    s._append_history(
        ChatMessage(
            role="summary", content="summarised",
            meta={
                "structured": {}, "covers_from_seq": 1,
                "covers_through_seq": tainted_seq,
            },
        )
    )

    assert not _narrowed(s), (
        "a real compaction watermark covering the evicted-but-latched entry's "
        "seq must clear narrowing -- the eviction-side latch must self-clear "
        "the same way the resident scan does, not stay permanently latched"
    )
