"""Tier 1/2: #5605 declares remote compaction progress reporting."""
from __future__ import annotations

from reyn.interfaces.inline.textual_chat.chrome import ctx_pane_lines
from reyn.interfaces.repl.read_model import (
    LOCAL_CHAT_READ_CAPABILITIES,
    REMOTE_CHAT_READ_CAPABILITIES,
    project_remote_snapshot,
    reported_snapshot_keys,
)


def test_compaction_progress_capability_distinguishes_local_and_remote() -> None:
    """Tier 1: LOCAL reports progress while REMOTE does not."""
    assert LOCAL_CHAT_READ_CAPABILITIES.compaction_progress_reported is True
    assert REMOTE_CHAT_READ_CAPABILITIES.compaction_progress_reported is False
    keys = reported_snapshot_keys(REMOTE_CHAT_READ_CAPABILITIES)
    assert keys["compaction_progress_reported"] is False


def test_remote_progress_is_explicitly_unreported() -> None:
    """Tier 2: remote Ctx chrome says unreported, not no fold yet."""
    snap = project_remote_snapshot({})
    assert snap["compaction_progress_reported"] is False
    assert snap["compaction_progress_raw"] is None
    lines = ctx_pane_lines(snap)
    folded = next(line for line in lines if line.startswith("folded"))
    assert "not reported on this connection" in folded


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
