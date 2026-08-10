"""Tier 1: contract — the recovery pair a caller builds via
``reyn.runtime.services.recovery.build_recovery`` must be wired to the SAME
``state_log`` and ``snapshot_path`` the session is given.

#3082 moved that construction out of ``Session.__init__`` to the composition
root. The move is only behaviour-preserving if the journal keeps appending
into the caller's own ``state_log`` instance and saving to the caller's own
``snapshot_path`` — a caller that resolves either of those twice, or passes a
different one to ``build_recovery`` than to ``Session``, silently splits the
WAL from the session that believes it owns it.

These two arms are ported from
``tests/scaffold/test_family2_recovery_bundle_byte_identical.py``, deleted by
that move per its own ``removed_by``. They are kept because a strip-falsify
showed the invariants were otherwise unguarded: rebinding the factory's
``build_recovery(..., state_log=...)`` argument to ``None`` left the whole
session/recovery/journal/wal selection GREEN. Symbol-reference counts are not
a guard — only an assertion of the property is.

Both prove the wiring from the PUBLIC surface (the state_log's own
``current_seq`` advancing; the snapshot file existing at the given path),
never by peeking at ``journal._state_log`` / ``journal._snapshot_path``.
"""
from __future__ import annotations

import pytest

from reyn.core.events.state_log import StateLog
from tests._support.agent_session import make_session


@pytest.mark.asyncio
async def test_journal_appends_into_the_exact_state_log_the_caller_passed(
    tmp_path, monkeypatch,
) -> None:
    """Tier 1: a journal append advances the caller's OWN ``state_log``.

    Falsified by binding the journal to any other StateLog (or to ``None``):
    ``current_seq`` on the instance the caller holds would not move.
    """
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "wiring.wal")
    session = make_session(agent_name="recovery-wiring-state-log", state_log=state_log)

    before = state_log.current_seq
    await session.journal.append_inbox(kind="test", payload={"y": 2})
    await state_log.flush()

    assert state_log.current_seq > before


@pytest.mark.asyncio
async def test_journal_saves_to_the_exact_snapshot_path_the_caller_passed(
    tmp_path, monkeypatch,
) -> None:
    """Tier 1: ``journal.save()`` writes to the caller's OWN ``snapshot_path``.

    Falsified by resolving the default path a second time inside the session:
    the file would appear under the re-derived location, not this one.
    """
    monkeypatch.chdir(tmp_path)
    snapshot_path = tmp_path / "explicit" / "snapshot.json"
    session = make_session(
        agent_name="recovery-wiring-snapshot-path", snapshot_path=snapshot_path,
    )

    await session.journal.append_inbox(kind="test", payload={"z": 3})
    await session.journal.save()

    assert snapshot_path.exists()
