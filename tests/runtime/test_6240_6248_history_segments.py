"""Tier 2: OS invariant -- #6240/#6248, the history segment layout itself.

Architect design (issue #6248 comments 5772696821 + 5772712797 -- the
second is the CURRENT ruling): ``<agent>/<sid>/history/`` holds an ACTIVE
segment (``history.jsonl``) plus zero or more SEALED segments
(``history-<min_seq>-<max_seq>-<s|n>.jsonl``). This file covers the two
properties ``test_5759_history_jsonl_gc.py`` does NOT (that file drives GC
against synthetic segment files; this one drives the REAL appender):

1. The appender seals the active segment exactly when its own size crosses
   the boundary, encoding the sealed segment's real seq range + summary
   presence into its OWN filename (deny/present pair).
2. New code never opens the pre-segment flat ``history.jsonl`` (the owner
   ruling this design encodes structurally, not by convention) -- and
   never deletes it either.

Real ``Session`` (via ``make_session``), real on-disk files throughout --
no mocks.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.history_segments import (
    SEGMENT_MAX_BYTES,
    list_sealed_segments,
    parse_sealed_segment_name,
)
from tests._support.agent_session import make_session


def _sealed(session) -> list:
    return list_sealed_segments(session.history_dir)


# ── ① boundary = size: seals exactly when crossed, never before ─────────


def test_appending_well_under_the_boundary_never_seals(tmp_path: Path) -> None:
    """Tier 2: deny-side -- a handful of small messages, nowhere near
    SEGMENT_MAX_BYTES, produce zero sealed segments; every message stays
    in the one active segment."""
    session = make_session(agent_name="alpha", workspace_base_dir=tmp_path)
    for i in range(20):
        session._append_history(ChatMessage(role="user", content=f"turn {i}"))

    assert _sealed(session) == []
    assert session.history_path.is_file()
    on_disk = [ln for ln in session.history_path.read_text().splitlines() if ln.strip()]
    assert [json.loads(ln)["content"] for ln in on_disk] == [f"turn {i}" for i in range(20)], (
        "every appended message must still sit in the one, unsealed active segment"
    )


def test_crossing_the_boundary_seals_exactly_one_segment(tmp_path: Path) -> None:
    """Tier 2: strip-falsifier target -- present-side. Appending enough
    content to cross SEGMENT_MAX_BYTES produces exactly ONE sealed
    segment (the content written before the crossing), while the active
    segment resumes fresh and keeps accepting new appends.

    Strip-falsify (in-file Edit only): commenting out the
    ``self._maybe_seal_active_history_segment()`` call at the top of
    ``Session._append_history`` turned this RED with a permanently empty
    ``_sealed(session)`` list (the active segment just kept growing past
    the boundary, single-file, pre-#6248 shape) -- restored (Edit),
    confirmed GREEN again."""
    session = make_session(agent_name="beta", workspace_base_dir=tmp_path)
    big = "x" * (SEGMENT_MAX_BYTES // 4)
    appended = 0
    while not _sealed(session):
        session._append_history(ChatMessage(role="user", content=big))
        appended += 1
        assert appended < 50, "sanity bound -- should have sealed well before this"

    sealed = _sealed(session)
    assert sealed, "expected at least one sealed segment"
    assert sealed[1:] == [], f"expected exactly one sealed segment, got {sealed}"
    assert sealed[0].min_seq == 1
    # #6248 ③: the seal check runs at the START of THIS ``appended``-th
    # call, against the size the PREVIOUS (appended - 1) writes already
    # produced -- so the sealed segment's own content is messages
    # 1..(appended - 1); THIS call's message is the one that triggered
    # the seal and lands in the FRESH active segment instead.
    assert sealed[0].max_seq == appended - 1
    assert sealed[0].has_summary is False

    # The active segment survived the seal and keeps accepting appends --
    # never left in a broken/unopenable state. It already holds this
    # call's own message (seq == appended); confirm a FOLLOWING append
    # lands right after it, in the same (not re-sealed) active segment.
    session._append_history(ChatMessage(role="user", content="after the seal"))
    assert session.history_path.is_file()
    after_seal_lines = [
        json.loads(ln) for ln in session.history_path.read_text().splitlines() if ln.strip()
    ]
    assert after_seal_lines[-1]["content"] == "after the seal"
    assert after_seal_lines[-1]["seq"] == appended + 1
    assert _sealed(session)[1:] == [], "a small follow-up append must not trigger a second seal"


def test_sealed_segment_name_records_summary_presence(tmp_path: Path) -> None:
    """Tier 2: a segment sealed WITH a ``role=\"summary\"`` line among its
    own content is named with the ``s`` flag; the sibling scenario with no
    summary is named ``n`` (already covered above) -- this is the
    deny/present pair for the has_summary FILENAME fact specifically."""
    session = make_session(agent_name="gamma", workspace_base_dir=tmp_path)
    big = "x" * (SEGMENT_MAX_BYTES // 4)
    session._append_history(ChatMessage(
        role="summary", content="s",
        meta={"structured": {}, "covers_through_seq": 1, "covers_from_seq": 1},
    ))
    while not _sealed(session):
        session._append_history(ChatMessage(role="user", content=big))

    sealed = _sealed(session)
    assert sealed, "expected at least one sealed segment"
    assert sealed[1:] == [], f"expected exactly one sealed segment, got {sealed}"
    assert sealed[0].has_summary is True
    assert sealed[0].min_seq == 1


def test_a_second_seal_produces_a_second_non_overlapping_segment(tmp_path: Path) -> None:
    """Tier 2: sealing is not a one-shot special case -- a session that
    crosses the boundary TWICE ends up with 2 sealed segments whose seq
    ranges do not overlap, both filename-parseable."""
    session = make_session(agent_name="delta", workspace_base_dir=tmp_path)
    big = "x" * (SEGMENT_MAX_BYTES // 4)
    while _sealed(session)[1:] == []:
        session._append_history(ChatMessage(role="user", content=big))

    sealed = sorted(_sealed(session), key=lambda s: s.min_seq)
    assert sealed[:2] and sealed[2:] == [], f"expected exactly two sealed segments, got {sealed}"
    assert sealed[0].max_seq < sealed[1].min_seq
    for seg in sealed:
        assert parse_sealed_segment_name(seg.path.name) is not None


# ── ② the pre-segment flat file: never opened, never deleted ────────────


def test_a_pre_segment_flat_history_file_is_never_read_by_a_new_session(
    tmp_path: Path,
) -> None:
    """Tier 2: #6248's owner-ruled boundary -- ``new_session.load_history()``
    must not surface a single line of a pre-existing FLAT
    ``<agent>/history.jsonl`` (no ``history/`` directory around it). This
    is the structural witness for "新しいコードは一度も開かない": the
    flat file carries content a segment-aware hydration would visibly
    include if it were ever opened.

    Strip-falsify (in-file Edit only): pointing ``Session.history_dir``'s
    own derivation at the pre-segment parent directory instead of
    ``history/`` underneath it (i.e. reverting to the flat-file lookup)
    turns this RED -- ``session.history`` would contain "legacy content"
    the flat file holds."""
    agent_dir = tmp_path / "alpha"
    agent_dir.mkdir(parents=True)
    flat = agent_dir / "history.jsonl"
    flat.write_text(
        json.dumps({"role": "user", "content": "legacy content", "seq": 1}) + "\n",
        encoding="utf-8",
    )
    before_mtime = flat.stat().st_mtime

    session = make_session(agent_name="alpha", workspace_base_dir=tmp_path)
    session.load_history()

    assert session.history == [], "a new session must start EMPTY, not hydrate the flat file"
    assert not any(
        getattr(m, "content", None) == "legacy content" for m in session.history
    )
    # ...and the flat file itself is untouched (never opened for write,
    # never deleted -- owner ruling: automatic deletion of a user's own
    # history is not the product's call).
    assert flat.exists()
    assert flat.stat().st_mtime == before_mtime
    assert json.loads(flat.read_text().splitlines()[0])["content"] == "legacy content"


def test_appending_after_a_flat_file_exists_writes_to_the_segment_dir_not_the_flat_file(
    tmp_path: Path,
) -> None:
    """Tier 2: present-side sibling -- a session with a pre-existing flat
    file still appends its OWN new turns into ``history/history.jsonl``,
    leaving the flat file's own byte count unchanged."""
    agent_dir = tmp_path / "beta"
    agent_dir.mkdir(parents=True)
    flat = agent_dir / "history.jsonl"
    flat.write_text(
        json.dumps({"role": "user", "content": "legacy", "seq": 1}) + "\n",
        encoding="utf-8",
    )
    flat_size_before = flat.stat().st_size

    session = make_session(agent_name="beta", workspace_base_dir=tmp_path)
    session._append_history(ChatMessage(role="user", content="brand new turn"))

    assert flat.stat().st_size == flat_size_before, "the flat file must never be appended to"
    assert session.history_path != flat
    assert session.history_path.parent.name == "history"
    on_disk = session.history_path.read_text().splitlines()
    assert json.loads(on_disk[0])["content"] == "brand new turn"
