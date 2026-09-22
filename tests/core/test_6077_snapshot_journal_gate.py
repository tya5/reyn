"""Tier 2: #6077 — SnapshotJournal's N-WAL-append gate on `save_nowait`, plus the
unconditional shutdown capture in `close()`.

Owner report (#6077, Windows, `owner-hit`): a multi-second pause after pressing Enter
before input clears. No measurement environment; architect ruling instead: stop
capturing a FULL-STATE snapshot (`to_payload()` + `deepcopy`) on EVERY WAL-recorded
mutation — capture+write once every N WAL appends, with an unconditional capture at
shutdown so a clean exit never leaves a trailing un-snapshotted tail. Recovery still
works because a crash mid-window replays the WAL tail onto the PRIOR snapshot — a
consistent prefix (this is `SnapshotJournal.save_nowait`'s own "criterion #2").

All witnesses observe the snapshot FILE from outside (existence / mtime / on-disk
content via `AgentSnapshot.load` or a raw read) — never a private counter/flag.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.services.snapshot_journal import SnapshotJournal

AGENT = "gate-agent"


def _journal(tmp_path: Path, *, interval: int) -> tuple[SnapshotJournal, StateLog, Path]:
    snap_path = tmp_path / "snapshot.json"
    log = StateLog(tmp_path / "state.wal")
    journal = SnapshotJournal(
        agent_name=AGENT, snapshot_path=snap_path, state_log=log,
        snapshot_interval=interval,
    )
    return journal, log, snap_path


# ---------------------------------------------------------------------------
# Deny-side: fewer than N WAL appends -> the snapshot file is not written.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fewer_than_n_appends_never_touch_the_snapshot_file(tmp_path):
    """Tier 2: N-1 out of N WAL-recorded mutations must do NO capture and NO write —
    observed on the on-disk FILE (existence + mtime), never a private counter.

    Strip-falsify: temporarily removed the gate in `save_nowait` (always called
    `_build_snapshot_write_job` + `submit_durable_nowait`, ignoring
    `_wal_appends_since_snapshot`/`_snapshot_interval` entirely). Observed RED with
    the literal error:
    ``AssertionError: the snapshot file must not exist before the Nth append -- the
    gate is not skipping non-triggering calls``
    Restored the gate immediately after (same turn) and re-ran GREEN.
    """
    interval = 5
    journal, log, snap_path = _journal(tmp_path, interval=interval)

    # 4 real mutations (not a wait loop) -- gate needs 5.
    await journal.append_inbox(kind="user", payload={"text": "m0"})
    await journal.append_inbox(kind="user", payload={"text": "m1"})
    await journal.append_inbox(kind="user", payload={"text": "m2"})
    await journal.append_inbox(kind="user", payload={"text": "m3"})
    await journal.flush()  # drain the WAL jobs; NO snapshot job should ever have been enqueued

    assert not snap_path.exists(), (
        "the snapshot file must not exist before the Nth append -- the gate is not "
        "skipping non-triggering calls"
    )
    await log.aclose()


@pytest.mark.asyncio
async def test_fewer_than_n_appends_leave_an_existing_snapshot_file_untouched(tmp_path):
    """Tier 2: deny-side variant with a PRE-EXISTING snapshot file (from `install()`,
    e.g. post-restore) — N-1 non-triggering appends must leave that file's mtime AND
    content exactly as they were, not merely "some file exists". A weaker
    "file exists" check alone could not catch a bug that re-wrote the SAME stale
    content on every call (still skipping the *real* capture but not truly idle).

    Strip-falsify: same break as the sibling deny-side test above (the gate
    ignored, always capturing+writing). Observed RED with the literal error:
    ``AssertionError: a non-triggering append must not rewrite the snapshot file
    at all`` (an `assert <newer mtime_ns> == <older mtime_ns>` failure). Restored
    immediately after (same turn) and re-ran GREEN."""
    interval = 4
    journal, log, snap_path = _journal(tmp_path, interval=interval)

    seed = AgentSnapshot.empty(AGENT, "main")
    seed.inbox.append({"id": "seed", "kind": "install", "payload": {"text": "seed"}})
    journal.install(seed)  # unconditional sync write -- the pre-existing file
    assert snap_path.exists()
    mtime_before = snap_path.stat().st_mtime_ns
    content_before = snap_path.read_text()

    # 3 real mutations (not a wait loop) -- gate needs 4.
    await journal.append_inbox(kind="user", payload={"text": "m0"})
    await journal.append_inbox(kind="user", payload={"text": "m1"})
    await journal.append_inbox(kind="user", payload={"text": "m2"})
    await journal.flush()

    assert snap_path.stat().st_mtime_ns == mtime_before, (
        "a non-triggering append must not rewrite the snapshot file at all"
    )
    assert snap_path.read_text() == content_before, (
        "a non-triggering append must not change the snapshot file's content"
    )
    await log.aclose()


# ---------------------------------------------------------------------------
# Present-side: the Nth append (and independently, shutdown) DOES write.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nth_append_writes_the_snapshot_file(tmp_path):
    """Tier 2: on exactly the Nth WAL append the gate fires -- the snapshot file is
    written with the current state. Proves the gate isn't satisfied by setting N so
    high the feature never fires (the companion to the deny-side test above).

    Strip-falsify: temporarily forced `save_nowait`'s gate check to `if True: return`
    (never resetting the counter or submitting the write job -- the gate stays closed
    forever). Observed RED with the literal error:
    ``AssertionError: the snapshot file must exist after the Nth append``
    Restored the real gate check immediately after (same turn) and re-ran GREEN.
    """
    interval = 3
    journal, log, snap_path = _journal(tmp_path, interval=interval)

    # exactly N real mutations (not a wait loop).
    await journal.append_inbox(kind="user", payload={"text": "m0"})
    await journal.append_inbox(kind="user", payload={"text": "m1"})
    await journal.append_inbox(kind="user", payload={"text": "m2"})
    await journal.flush()

    assert snap_path.exists(), "the snapshot file must exist after the Nth append"
    on_disk = AgentSnapshot.load(AGENT, snap_path)
    assert [m["payload"]["text"] for m in on_disk.inbox] == ["m0", "m1", "m2"]
    await log.aclose()


@pytest.mark.asyncio
async def test_close_writes_unconditionally_below_the_gate(tmp_path):
    """Tier 2: process-shutdown-equivalent (`SnapshotJournal.close()`) captures+writes
    UNCONDITIONALLY, independent of the N-append counter -- even when the gate was
    never reached. This is what stops a clean exit from paying an avoidable replay on
    the NEXT restart."""
    interval = 20  # far above the 2 appends below -- close() must still write
    journal, log, snap_path = _journal(tmp_path, interval=interval)

    await journal.append_inbox(kind="user", payload={"text": "x"})
    await journal.append_inbox(kind="user", payload={"text": "y"})
    await journal.flush()
    assert not snap_path.exists(), "sanity: the gate must not have fired yet (2 < 20)"

    await journal.close()

    assert snap_path.exists(), "close() must write the snapshot file unconditionally"
    on_disk = AgentSnapshot.load(AGENT, snap_path)
    assert [m["payload"]["text"] for m in on_disk.inbox] == ["x", "y"]
    await log.aclose()


# ---------------------------------------------------------------------------
# Recovery-feature truncate-falsify (CLAUDE.md): set X, truncate the WAL past
# X's events, reconstruct, assert X survives -- a consistent prefix even though
# the on-disk snapshot lags behind the live WAL by construction of the gate.
# ---------------------------------------------------------------------------


def _truncate_wal_to(wal_path: Path, keep_through_seq: int) -> None:
    """Rewrite the WAL file keeping only entries with seq <= keep_through_seq --
    simulates a crash whose fsync'd tail stopped exactly there."""
    lines = [ln for ln in wal_path.read_text().splitlines() if ln.strip()]
    kept = [ln for ln in lines if json.loads(ln)["seq"] <= keep_through_seq]
    wal_path.write_text("\n".join(kept) + ("\n" if kept else ""))


@pytest.mark.asyncio
async def test_truncate_falsify_state_captured_at_the_gate_survives(tmp_path):
    """Tier 2: recovery-feature truncate-falsify — set X = the state at the gate's own
    trigger point (3 appends, interval=3 -> one snapshot capture+write), truncate the
    WAL past X's own events (drop everything after "c"'s seq, including a later
    un-gated append "d"), reconstruct (durable snapshot + WAL-tail replay), and assert
    X survives -- exactly ["a","b","c"], neither less (a lost capture) nor more (a
    replay of truncated-away entries)."""
    interval = 3
    journal, log, snap_path = _journal(tmp_path, interval=interval)

    await journal.append_inbox(kind="user", payload={"text": "a"})
    await journal.append_inbox(kind="user", payload={"text": "b"})
    await journal.append_inbox(kind="user", payload={"text": "c"})  # 3rd -> gate fires
    await journal.flush()
    assert snap_path.exists()
    gate_seq = AgentSnapshot.load(AGENT, snap_path).applied_seq

    # A further, un-gated mutation (counter resets to 1/3 after the trigger above) --
    # durable in the WAL, but the snapshot file is NOT updated for it (deny-side).
    await journal.append_inbox(kind="user", payload={"text": "d"})
    await journal.flush()
    await log.aclose()  # stop the worker before hand-editing the WAL file on disk

    # Simulate a crash whose durable tail stopped exactly at X (gate_seq) -- "d"'s
    # WAL entry never survived the crash either.
    _truncate_wal_to(log.path, keep_through_seq=gate_seq)

    # Reconstruct: durable snapshot (X) + replay the (now-empty) WAL tail past it.
    fresh_log = StateLog(log.path)
    rebuilt = AgentSnapshot.load(AGENT, snap_path)
    rebuilt.apply_events(list(fresh_log.iter_from(rebuilt.applied_seq)))
    texts = [m["payload"]["text"] for m in rebuilt.inbox]

    assert texts == ["a", "b", "c"], (
        f"X (the state captured at the gate's own trigger) must survive truncation "
        f"past its own events unchanged; got {texts}"
    )
    await fresh_log.aclose()


@pytest.mark.asyncio
async def test_truncate_falsify_replay_onto_the_prior_snapshot_recovers_the_ungated_tail(
    tmp_path,
):
    """Tier 2: recovery-feature truncate-falsify (the companion consistent-prefix case,
    architect's criterion #2): if the crash's durable tail extends PAST the gate's own
    last capture (the un-gated "d" survives the crash in the WAL, only the snapshot
    lags), reconstruct must replay it onto the PRIOR (stale) snapshot and recover the
    full consistent prefix ["a","b","c","d"] -- the gate skipping "d"'s own capture
    never loses the mutation itself, only the redundant snapshot write."""
    interval = 3
    journal, log, snap_path = _journal(tmp_path, interval=interval)

    await journal.append_inbox(kind="user", payload={"text": "a"})
    await journal.append_inbox(kind="user", payload={"text": "b"})
    await journal.append_inbox(kind="user", payload={"text": "c"})  # gate fires
    await journal.append_inbox(kind="user", payload={"text": "d"})  # gate does NOT fire (1/3)
    await journal.flush()
    await log.aclose()

    # No truncation this time -- "d"'s WAL entry is fully durable and survives intact.
    fresh_log = StateLog(log.path)
    rebuilt = AgentSnapshot.load(AGENT, snap_path)  # the STALE snapshot -- only a,b,c
    assert [m["payload"]["text"] for m in rebuilt.inbox] == ["a", "b", "c"], (
        "sanity: the on-disk snapshot must still be the gate-stale one, not d"
    )
    rebuilt.apply_events(list(fresh_log.iter_from(rebuilt.applied_seq)))
    texts = [m["payload"]["text"] for m in rebuilt.inbox]

    assert texts == ["a", "b", "c", "d"], (
        f"replaying the surviving WAL tail onto the prior (stale) snapshot must "
        f"recover the full consistent prefix; got {texts}"
    )
    await fresh_log.aclose()
