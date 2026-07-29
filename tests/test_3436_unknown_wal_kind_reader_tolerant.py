"""Tests: reader tolerance for a WAL kind absent from WAL_EVENT_KINDS (#3436).

Tier 2c: OS invariant — general form of "a kind removed (or never present) in
WAL_EVENT_KINDS still replays safely" — the class #2507 (skill_*) and #3436
(task_subscribed/task_rebound) are both instances of. #2507 got a bespoke test
(``test_removed_skill_wal_kinds_recovery_safe`` in test_state_log_truncate.py,
hardcoding the ``skill_*`` literals); #3436 measured the identical property for
a second, unrelated kind pair and would otherwise need its own bespoke
measurement too. This module closes the CLASS instead of adding a third
instance: it never hardcodes any specific out-of-vocabulary kind name (real,
removed, or currently-legal-but-unregistered) — it draws one from OUT OF
BAND (a uuid4-suffixed synthetic string, asserted absent from
``WAL_EVENT_KINDS`` as a sanity check) — so a THIRD WAL-kind removal needs
neither a new measurement pass nor a new test: this module already covers
"any kind absent from WAL_EVENT_KINDS", not one specific kind.

Covers all three reader paths the #3436 measurement pass exercised:
``StateLog.iter_from``, ``WalTailReader.poll``, ``AgentSnapshot.apply_events``
(plus the write-side reject, the other half of the contract).

Disposition of ``test_removed_skill_wal_kinds_recovery_safe`` (#2507):
REMOVED from ``test_state_log_truncate.py`` in the same PR that adds this
module. Its two claims are both strictly subsumed here: (a) write-side reject
of an unregistered kind is generalised by
``test_write_side_rejects_unregistered_kind`` below (the removed ``skill_*``
literals added no guarantee beyond "any kind not in WAL_EVENT_KINDS raises" —
already true for whichever synthetic kind this module draws); (b) read-side
tolerance + neighbor-preservation is generalised by
``test_iter_from_tolerates_unregistered_kind_and_keeps_neighbors``. Nothing in
the removed test exercised ``skill_*``-specific reconstruction semantics (the
``_apply_one`` dispatch has no ``skill_*`` branch either, before or after
#2507) — so no bespoke coverage is lost.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import WAL_EVENT_KINDS, StateLog, WalTailReader


def _unregistered_kind() -> str:
    """A kind name guaranteed to be outside ``WAL_EVENT_KINDS`` today, drawn
    from OUT OF the legal-vocabulary namespace (uuid4-suffixed) rather than a
    guessed/plausible future name — so it can never collide with a kind this
    repo later legitimately registers."""
    kind = f"unregistered_kind_{uuid.uuid4().hex}"
    assert kind not in WAL_EVENT_KINDS  # sanity: genuinely out-of-vocabulary
    return kind


def test_write_side_rejects_unregistered_kind(tmp_path: Path) -> None:
    """Tier 2c: append() rejects ANY kind absent from WAL_EVENT_KINDS — the
    write-side half of the class (a typo or a stale writer for a removed kind
    cannot silently fragment the recovery vocabulary)."""
    kind = _unregistered_kind()
    log = StateLog(tmp_path / "wal.jsonl")

    async def go() -> None:
        try:
            await log.append(kind, target="a1")
        except ValueError as exc:
            assert "unknown WAL event kind" in str(exc)
        else:
            raise AssertionError("append() must reject an unregistered kind")

    asyncio.run(go())


def test_iter_from_tolerates_unregistered_kind_and_keeps_neighbors(
    tmp_path: Path,
) -> None:
    """Tier 2c: StateLog.iter_from does not validate `kind` against
    WAL_EVENT_KINDS at read time — a hand-written unregistered-kind line is
    read through with NO exception, and the KEPT entries before/after it are
    not lost (the #2259/#2260 data-loss class this whole gate exists to close).

    FALSIFICATION (strip-falsify, run manually before this PR, RED observed —
    see PR body): gating ``iter_from`` on ``WAL_EVENT_KINDS`` turns this into
    an exception that drops the unregistered line AND every entry the caller
    would have seen after it in the same generator.
    """
    kind = _unregistered_kind()
    wal = tmp_path / "wal.jsonl"
    log = StateLog(wal)

    async def go() -> int:
        s0 = await log.append("inbox_put", target="a1", msg_id="m0", msg_kind="k")
        await log.flush()
        return s0

    s0 = asyncio.run(go())
    with wal.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": s0 + 1, "kind": kind, "target": "a1"}) + "\n")
        f.write(
            json.dumps(
                {
                    "seq": s0 + 2,
                    "kind": "inbox_put",
                    "target": "a1",
                    "msg_id": "m2",
                    "msg_kind": "k",
                }
            )
            + "\n"
        )

    entries = list(log.iter_from(0))  # must not raise
    kinds = [e.get("kind") for e in entries]
    seqs = {e["seq"] for e in entries}
    assert kind in kinds  # unregistered-kind line reads through
    assert seqs == {s0, s0 + 1, s0 + 2}  # neighbors before AND after survive


def test_wal_tail_reader_tolerates_unregistered_kind(tmp_path: Path) -> None:
    """Tier 2c: WalTailReader.poll() has the same tolerance as iter_from — it
    does not gate on WAL_EVENT_KINDS either, so a live tail-follower (the
    #2939 incremental-poll consumer) sees an unregistered-kind entry exactly
    like any other kind: no exception, delta cursor still advances past it."""
    kind = _unregistered_kind()
    wal = tmp_path / "wal.jsonl"
    log = StateLog(wal)
    reader = WalTailReader(log)

    async def append_first() -> int:
        s0 = await log.append("inbox_put", target="a1", msg_id="m0", msg_kind="k")
        await log.flush()
        return s0

    s0 = asyncio.run(append_first())
    first_batch, _restarted0 = reader.poll()
    list(first_batch)  # drain (contract: consume a batch before polling again)

    with wal.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": s0 + 1, "kind": kind, "target": "a1"}) + "\n")
        f.write(
            json.dumps(
                {
                    "seq": s0 + 2,
                    "kind": "inbox_put",
                    "target": "a1",
                    "msg_id": "m2",
                    "msg_kind": "k",
                }
            )
            + "\n"
        )

    entries, _restarted = reader.poll()
    entries = list(entries)  # must not raise
    kinds = [e.get("kind") for e in entries]
    seqs = {e["seq"] for e in entries}
    assert kind in kinds
    assert seqs == {s0 + 1, s0 + 2}


def test_agent_snapshot_apply_events_noops_unregistered_kind() -> None:
    """Tier 2c: AgentSnapshot.apply_events treats an unregistered kind exactly
    like a currently-registered-but-dispatch-unhandled kind: applied_seq
    advances (seen, skip), no exception is raised, and no other snapshot
    field mutates. Matches the documented fall-through in ``_apply_one``'s
    trailing comment: "Unknown kinds: no-op (forward compatibility for
    future kinds)."""
    kind = _unregistered_kind()
    snap = AgentSnapshot.empty("agent_x")
    snap.apply_events(
        [
            {
                "kind": "inbox_put",
                "seq": 1,
                "target": "agent_x",
                "msg_id": "m0",
                "msg_kind": "k",
            },
            {"kind": kind, "seq": 2, "target": "agent_x"},
            {
                "kind": "inbox_put",
                "seq": 3,
                "target": "agent_x",
                "msg_id": "m1",
                "msg_kind": "k",
            },
        ]
    )
    assert snap.applied_seq == 3  # advanced past the unregistered-kind entry too
    assert [m["id"] for m in snap.inbox] == ["m0", "m1"]  # neighbors both applied
    assert snap.outstanding_interventions == {}
    assert snap.pending_chains == {}
