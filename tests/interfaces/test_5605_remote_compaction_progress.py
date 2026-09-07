"""Tier 1/2: #5605 declared remote compaction progress UNREPORTED; #5885
put it on the wire, so this file now pins that REMOTE reports it too."""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import ctx_pane_lines
from reyn.interfaces.repl.read_model import (
    LOCAL_CHAT_READ_CAPABILITIES,
    REMOTE_CHAT_READ_CAPABILITIES,
    project_remote_snapshot,
    reported_snapshot_keys,
)


def test_compaction_progress_capability_is_reported_on_both_transports() -> None:
    """Tier 1: LOCAL and REMOTE both report progress (#5885 flipped REMOTE;
    #5605 had pinned it False while nothing carried it on the wire)."""
    assert LOCAL_CHAT_READ_CAPABILITIES.compaction_progress_reported is True
    assert REMOTE_CHAT_READ_CAPABILITIES.compaction_progress_reported is True
    keys = reported_snapshot_keys(REMOTE_CHAT_READ_CAPABILITIES)
    assert keys["compaction_progress_reported"] is True


def test_remote_progress_reads_the_wire_value() -> None:
    """Tier 2: the remote snapshot carries the wire's ``compaction_progress_
    raw`` verbatim and the Ctx chrome renders the REAL fold state from it —
    never "not reported" any more."""
    snap = project_remote_snapshot({
        "ctx_window": 1000, "ctx_used": 100,
        "compaction_progress_raw": {"is_compacting": False, "persisted_covers_through_seq": 42},
    })
    assert snap["compaction_progress_reported"] is True
    assert snap["compaction_progress_raw"] == {
        "is_compacting": False, "persisted_covers_through_seq": 42,
    }
    lines = ctx_pane_lines(snap)
    folded = next(line for line in lines if line.startswith("folded"))
    assert "42" in folded and "not reported" not in folded


def test_remote_progress_absent_on_the_wire_is_none_not_fabricated() -> None:
    """Tier 2: a pre-STATE_SNAPSHOT remote (nothing on the wire yet) reads
    ``None`` — the app treats that as "not compacting", never as a
    fabricated figure."""
    snap = project_remote_snapshot({})
    assert snap["compaction_progress_raw"] is None


def test_reported_local_progress_preserves_the_real_fold_state() -> None:
    """Tier 2: a reported progress dict still renders measured state."""
    snap = {
        "ctx_window": 1000,
        "ctx_used": 100,
        "compaction_progress_reported": True,
        "compaction_progress_raw": {"persisted_covers_through_seq": None},
    }
    lines = ctx_pane_lines(snap)
    folded = next(line for line in lines if line.startswith("folded"))
    assert "no recovery fold persisted yet" in folded
    assert "not reported" not in folded
