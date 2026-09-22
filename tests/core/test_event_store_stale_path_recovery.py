"""Tier 2: EventStore stale-path recovery when the events directory is deleted
by an external process while the EventStore is still alive.

Regression for B35 W1 ablation condition C: dogfood driver wipes
.reyn/events/ between scenarios while `reyn web` is live, causing
EventStore._active to hold a stale path. The next write raised
FileNotFoundError. This test suite validates the fix.

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- No unittest.mock / MagicMock / AsyncMock / patch.
- Real file operations with pytest tmp_path.
- Test docstring first line declares Tier.
"""
from __future__ import annotations

import shutil
import stat

import pytest

from reyn.core.events.event_store import EventStore
from reyn.schemas.models import Event

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_event(kind: str = "test_event") -> Event:
    return Event(type=kind, data={"x": 1})


# ---------------------------------------------------------------------------
# Test 1: basic recovery — rmtree while alive, next write succeeds
# ---------------------------------------------------------------------------


def test_stale_path_recovery_after_rmtree(tmp_path: pytest.TempPathFactory) -> None:
    """Tier 2: EventStore recovers when the events directory is deleted externally.

    Steps:
    1. Create EventStore, write one event — confirms normal path works.
    2. shutil.rmtree(events_dir) while the EventStore is still alive.
    3. Write another event — must succeed (recovery fires, new file created).
    4. Confirm the second event is readable from the new file.
    """
    events_dir = tmp_path / "events"
    store = EventStore(events_dir)

    # Step 1: normal write — establishes _active path.
    ev1 = _make_event("before_wipe")
    store.write(ev1)
    assert store.active_path is not None
    assert store.active_path.exists(), "sanity: active file must exist after first write"

    # Step 2: external deletion.
    shutil.rmtree(events_dir)
    assert not events_dir.exists(), "sanity: rmtree must have removed the directory"

    # Step 3: write after wipe — should NOT raise FileNotFoundError.
    ev2 = _make_event("after_wipe")
    store.write(ev2)  # recovery fires here

    # Step 4: new active file exists and contains the recovered event.
    assert store.active_path is not None, "active_path must be set after recovery"
    assert store.active_path.exists(), "recovered active file must exist on disk"

    contents = store.active_path.read_text(encoding="utf-8")
    assert "after_wipe" in contents, (
        f"recovered file must contain the second event; got: {contents!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: bounded retry — second failure re-raises
# ---------------------------------------------------------------------------


def test_stale_path_recovery_bounded_retry(tmp_path: pytest.TempPathFactory) -> None:
    """Tier 2: EventStore bounded retry — if recovery also fails, re-raise.

    #6077 提案 6 update: `EventStore` now holds a session-lifetime append
    handle (`_ensure_active_handle`) instead of open()/close()-ing per
    write, so this scenario now exercises a SINGLE failing open attempt,
    not the old two-attempt "recovery also fails" shape (`_write_owned` ->
    `_ensure_active_handle`: the held handle's inode no longer matches
    `active_path` after the unlink below, so it closes it and tries ONE
    fresh `path.open("a")` against the now-unwritable directory — that
    single attempt raises PermissionError directly). The assertion
    (exception propagates) and the exception tuple accepted are unchanged;
    only the internal mechanism producing it is.

    Approach:
    - Write event 1 (normal) — opens the session-lifetime handle.
    - Delete the active file directly (not rmtree) so the directory still
      exists but the file is gone (its inode is unlinked from under the
      held-open handle).
    - Make the month subdirectory unwritable so the handle's own recovery
      open (`_ensure_active_handle`'s `path.open("a")`, triggered by the
      inode-mismatch it detects) fails.
    - Assert that the exception propagates.

    Cleanup: restore directory permissions so tmp_path cleanup works.
    """
    events_dir = tmp_path / "events"
    store = EventStore(events_dir)

    # Step 1: normal write.
    store.write(_make_event("initial"))
    active = store.active_path
    assert active is not None and active.exists()

    # Capture the month dir created by _begin_new_active_file/_ensure_active_handle.
    month_dir = active.parent

    # Step 2: delete only the active file (not the directory).
    active.unlink()
    assert not active.exists()

    # Step 3: make the month directory unwritable so the retry cannot create
    # a new file inside it.
    month_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # r-x, no write

    try:
        # Step 4: write must re-raise because retry also fails.
        with pytest.raises((FileNotFoundError, PermissionError, OSError)):
            store.write(_make_event("should_fail"))
    finally:
        # Restore permissions so pytest tmp_path cleanup can remove the tree.
        month_dir.chmod(stat.S_IRWXU)
