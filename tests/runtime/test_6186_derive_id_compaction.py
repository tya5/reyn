"""Tier 1: #6186's compaction UPDATE axis — `outbox.py`'s
`_derive_id_and_parent_id` gains an `episode:{compaction_episode_seq}`
branch, checked FIRST of all (ahead of #6213's own `tool:`/`call:`/
`turn:` branches) — reyn's own fact (`Session._compaction_episode_seq`,
never a third party's), so this is the SAME `kind:value` vocabulary
extended with a new namespace, not a 6th identifier kind (#6186's own
"answer in one system" ruling).

Both producers (`lifecycle_forwarder.py`'s 5 marker-emitting handlers,
`app.py`'s own progress-entry row) are `kind="system"` — `kind` alone
cannot distinguish them, so the branch is gated on
`compaction_episode_marker`'s presence instead.
"""
from __future__ import annotations

from reyn.runtime.outbox import _derive_id_and_parent_id


def test_marker_frame_parent_id_is_episode_prefixed_seq():
    """Tier 1: a marker frame (compaction_episode_marker=True) gets no
    id of its own — only a parent_id naming the episode it belongs to."""
    id_, parent_id = _derive_id_and_parent_id(
        kind="system",
        meta={"compaction_episode_seq": 3, "compaction_episode_marker": True},
    )
    assert id_ is None
    assert parent_id == "episode:3"


def test_progress_entry_row_id_is_episode_prefixed_seq():
    """Tier 1: the progress-entry row itself (no marker flag) gets its
    OWN id — the thing marker frames' parent_id will later match
    against."""
    id_, parent_id = _derive_id_and_parent_id(
        kind="system",
        meta={"compaction_episode_seq": 3},
    )
    assert id_ == "episode:3"
    assert parent_id is None


def test_different_episode_seq_produces_a_different_value():
    """Tier 1: LOAD-BEARING — the exact structural property absorption
    depends on. strip: hardcode the episode prefix without the seq
    (e.g. always "episode:current") -- both values below would collapse
    to the same string despite naming different episodes."""
    _, parent_a = _derive_id_and_parent_id(
        kind="system",
        meta={"compaction_episode_seq": 1, "compaction_episode_marker": True},
    )
    _, parent_b = _derive_id_and_parent_id(
        kind="system",
        meta={"compaction_episode_seq": 2, "compaction_episode_marker": True},
    )
    assert parent_a != parent_b


def test_compaction_branch_is_checked_before_dispatch_id_and_call_id():
    """Tier 1: accept④'s own witness — a message that (hypothetically)
    carried BOTH compaction_episode_seq and call_id/dispatch_id would
    still resolve through the compaction branch, since it is checked
    FIRST. In practice no real producer stamps both (compaction markers
    carry neither call_id nor chain_id, verified directly against
    lifecycle_forwarder.py's own _compaction_marker_meta()), so this
    test exercises the STRUCTURAL ordering, not a real message shape."""
    id_, parent_id = _derive_id_and_parent_id(
        kind="tool_call_started",
        meta={
            "compaction_episode_seq": 5,
            "compaction_episode_marker": True,
            "dispatch_id": "abc123",
            "call_id": "resp-1",
        },
    )
    assert id_ is None
    assert parent_id == "episode:5"


def test_no_compaction_episode_seq_falls_through_unaffected():
    """Tier 1: regression guard — a message with NO compaction_episode_
    seq (the overwhelming majority of messages) gets the EXACT pre-#6186
    derivation, byte-identical. This fix is additive, never a
    replacement — the new branch cannot change any non-compaction
    message's id/parent_id, because it is gated on a key nothing else
    stamps."""
    id_, parent_id = _derive_id_and_parent_id(
        kind="tool_call_started", meta={"dispatch_id": "abc123", "call_id": "resp-1"},
    )
    assert id_ == "tool:abc123"
    assert parent_id == "call:resp-1"

    id_, parent_id = _derive_id_and_parent_id(kind="agent", meta={"call_id": "resp-1"})
    assert id_ == "call:resp-1"

    id_, parent_id = _derive_id_and_parent_id(kind="system", meta={})
    assert (id_, parent_id) == (None, None)


def test_compaction_episode_seq_of_zero_is_a_real_episode_not_absent():
    """Tier 1: episode_seq 0 is a valid FIRST episode (Session's own
    counter starts at 0 per its construction-site comment) — the branch
    must gate on `is not None`, never on truthiness, or the very first
    episode of a session would silently never get an id."""
    id_, parent_id = _derive_id_and_parent_id(
        kind="system", meta={"compaction_episode_seq": 0},
    )
    assert id_ == "episode:0"
