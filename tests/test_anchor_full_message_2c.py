"""Tier 2: OS invariant — AnchorStore full-message capture for edit-prefill (#1533 2c).

The rewind-timeline anchor (truncated ~80c) is for *display*; the 2c edit flow
needs the **full** original user message to prefill + re-run (truncated re-run
would lose the original tail = correctness bug). The full message is in hand at
`cut_generation` time, so it is persisted additively alongside the truncated
anchor (robust vs fragile after-the-fact WAL/history mining). Turn checkpoints
only (plan-step/phase pass no user message). Back-compatible: legacy str-valued
anchor files load with full = "" (edit-prefill degrades to empty = manual re-type).

Real AnchorStore + SnapshotJournal (no mocks); mirrors tests/test_anchor_text_1547.py.
"""
from __future__ import annotations

import json

import pytest

from reyn.chat.services.snapshot_journal import SnapshotJournal
from reyn.core.events.anchor_store import AnchorStore
from reyn.core.events.snapshot_generations import SnapshotGenerationStore
from reyn.core.events.state_log import StateLog

# ── AnchorStore: full alongside truncated ─────────────────────────────────────


def test_capture_stores_full_and_truncated_independently(tmp_path):
    """Tier 2: capture(anchor, full) → get = truncated display, get_full = full original."""
    store = AnchorStore(tmp_path / "anchors.json")
    full = "fix the parser bug in the tokenizer and also " + ("x" * 100)
    store.capture(10, "fix the parser bug…", full=full)

    assert store.get(10) == "fix the parser bug…"   # truncated display unchanged
    assert store.get_full(10) == full               # full original recovered


def test_get_full_unknown_seq_is_empty(tmp_path):
    """Tier 2: get_full for an unrecorded seq returns "" (slot-in-unconditionally)."""
    store = AnchorStore(tmp_path / "anchors.json")
    store.capture(10, "hi", full="hi there")
    assert store.get_full(99) == ""


def test_full_persists_across_instances(tmp_path):
    """Tier 2: the full message is durable — a fresh store over the same path reads it."""
    AnchorStore(tmp_path / "a.json").capture(5, "kept", full="kept in full glory")
    assert AnchorStore(tmp_path / "a.json").get_full(5) == "kept in full glory"


def test_legacy_str_valued_file_degrades_get_full_to_empty(tmp_path):
    """Tier 2: a pre-2c anchors file (str values) loads with get intact, get_full = "".

    Back-compat: old files store ``{seq: truncated_str}``. The truncated display
    must still work (``get``); ``get_full`` degrades to "" (edit-prefill empty =
    manual re-type, acceptable) rather than crashing.
    """
    path = tmp_path / "anchors.json"
    path.write_text(json.dumps({"7": "legacy truncated text"}), encoding="utf-8")
    store = AnchorStore(path)
    assert store.get(7) == "legacy truncated text"   # display still works
    assert store.get_full(7) == ""                    # no full in legacy → degrade


def test_prune_drops_full_with_the_entry(tmp_path):
    """Tier 2: prune_below drops the whole entry (truncated + full) below the floor."""
    store = AnchorStore(tmp_path / "anchors.json")
    store.capture(10, "a", full="a-full")
    store.capture(20, "b", full="b-full")
    store.prune_below(20)
    assert store.get(10) == "" and store.get_full(10) == ""   # 10 gone entirely
    assert store.get_full(20) == "b-full"                      # 20 retained


# ── cut_generation threads full_message ───────────────────────────────────────


@pytest.mark.asyncio
async def test_cut_generation_captures_full_message(tmp_path):
    """Tier 2: cut_generation(anchor, full_message) records both under the boundary seq."""
    log = StateLog(tmp_path / "wal")
    seq = await log.append("inbox_put", target="a", msg_id="m", msg_kind="user", payload={})
    journal = SnapshotJournal(
        agent_name="a", snapshot_path=tmp_path / "snap.json", state_log=log,
        generation_store=SnapshotGenerationStore("a", tmp_path / "gens"),
    )
    anchors = AnchorStore(tmp_path / "anchors.json")
    journal.set_anchor_store(anchors)
    journal.snapshot.applied_seq = seq

    await journal.cut_generation(anchor="trunc…", full_message="the full original message")
    assert anchors.get(seq) == "trunc…"
    assert anchors.get_full(seq) == "the full original message"


@pytest.mark.asyncio
async def test_cut_generation_no_anchor_captures_nothing(tmp_path):
    """Tier 2: a no-anchor cut (plan-step/phase) stores neither truncated nor full."""
    log = StateLog(tmp_path / "wal")
    seq = await log.append("inbox_put", target="a", msg_id="m", msg_kind="user", payload={})
    journal = SnapshotJournal(
        agent_name="a", snapshot_path=tmp_path / "snap.json", state_log=log,
        generation_store=SnapshotGenerationStore("a", tmp_path / "gens"),
    )
    anchors = AnchorStore(tmp_path / "anchors.json")
    journal.set_anchor_store(anchors)
    journal.snapshot.applied_seq = seq

    await journal.cut_generation()               # no anchor (non-turn checkpoint)
    assert anchors.get(seq) == "" and anchors.get_full(seq) == ""
