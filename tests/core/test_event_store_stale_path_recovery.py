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

    Simulates two consecutive failures without mocks by making the parent
    directory unwritable after the first write. The recovery path calls
    _open_new_file() which calls mkdir(); if the parent is unwritable the
    mkdir() raises PermissionError (not FileNotFoundError), so we need to
    test the case where _open_new_file() itself triggers a failure on the
    second open.

    Approach:
    - Write event 1 (normal).
    - Delete the active file directly (not rmtree) so the directory still
      exists but the file is gone. This triggers FileNotFoundError on write.
    - Make the month subdirectory unwritable so _open_new_file().touch()
      also fails — simulating a second consecutive failure.
    - Assert that the exception propagates (second failure re-raises).

    Cleanup: restore directory permissions so tmp_path cleanup works.
    """
    events_dir = tmp_path / "events"
    store = EventStore(events_dir)

    # Step 1: normal write.
    store.write(_make_event("initial"))
    active = store.active_path
    assert active is not None and active.exists()

    # Capture the month dir created by _open_new_file.
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
