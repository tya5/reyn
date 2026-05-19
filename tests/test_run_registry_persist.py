"""Tier 2: RunRegistry persistence — issue #267 Gap 5 Phase 1.

Pins the persistence contract added to ``RunRegistry`` so a server-
process restart can reload the A2A async-task lifecycle state instead
of losing it to in-memory volatility (= the structural prerequisite
for #270 pending-op framework's MCP-side restart behaviour + closes
the gap that left A2A peer routing half-restored in #268's
origin-pinned model).

Pins:

  1. ``persist_path=None`` (default) preserves pre-#267 in-memory-only
     behaviour — no disk writes, restore is a no-op.
  2. With ``persist_path=...`` set, every mutation (create / update /
     attach_task is volatile / answer_intervention / cancel /
     append_event / remove) writes the snapshot atomically.
  3. The atomic-rename pattern (= tmp file + ``replace()``) avoids a
     half-written file being read by a concurrent restore.
  4. Round-trip preserves the JSON-safe fields verbatim, including
     pending_intervention via ``UserIntervention.to_dict``.
  5. Restored entries have ``task=None`` (= cannot resurrect dead
     ``asyncio.Task``).
  6. Restored ``pending_intervention`` has a **fresh future** so a new
     awaiter can pick it up (= ``UserIntervention.from_dict`` allocates
     a new future via ``__post_init__``).
  7. Corrupt / malformed snapshot file is tolerated — registry starts
     empty + warns rather than crashing.

No mocks. Real ``RunRegistry`` writes + reads on tmp_path.

Tier 2 — not a Tier-3 LLM-replay test, no scaffold churn expected.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.user_intervention import InterventionAnswer, UserIntervention
from reyn.web.run_registry import RunEntry, RunRegistry

# ── 1. Default (no persist_path) — legacy in-memory behaviour ──────────


def test_default_construction_does_not_persist_to_disk(tmp_path: Path) -> None:
    """Tier 2: ``RunRegistry()`` without ``persist_path`` is in-memory only.

    Mutations succeed but no file is written. Important for tests +
    direct callers that don't need persistence and shouldn't pay the
    disk-IO cost.
    """
    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")
    assert entry.run_id in {e.run_id for e in registry.list()}

    # No file should appear under tmp_path because we didn't supply
    # persist_path.
    assert list(tmp_path.iterdir()) == []


# ── 2. Persist + restore round-trip (= core contract) ───────────────────


def test_create_writes_snapshot_to_persist_path(tmp_path: Path) -> None:
    """Tier 2: ``create()`` triggers an atomic snapshot write.

    The snapshot file appears with the entry's data after a single
    ``create()`` call.
    """
    persist_path = tmp_path / "run_registry.json"
    registry = RunRegistry(persist_path=persist_path)
    entry = registry.create(
        agent_name="demo",
        chain_id="chain-A",
        webhook_url="http://peer.example/webhook",
    )

    assert persist_path.exists()
    data = json.loads(persist_path.read_text(encoding="utf-8"))
    assert entry.run_id in data
    assert data[entry.run_id]["agent_name"] == "demo"
    assert data[entry.run_id]["chain_id"] == "chain-A"
    assert data[entry.run_id]["status"] == "running"
    assert data[entry.run_id]["webhook_url"] == "http://peer.example/webhook"


def test_restore_from_existing_snapshot_repopulates_runs(tmp_path: Path) -> None:
    """Tier 2: when an existing snapshot file is present at construction,
    the registry reloads the entries.

    This is the central contract — process restart can pick up where
    it left off instead of starting from an empty registry.
    """
    persist_path = tmp_path / "run_registry.json"
    # Phase 1: populate + close (= process death simulation).
    registry_a = RunRegistry(persist_path=persist_path)
    entry = registry_a.create(agent_name="demo", chain_id="chain-A")
    registry_a.update(entry.run_id, status="input-required", question="Continue?")
    registry_a.append_event(entry.run_id, {"type": "phase", "name": "greet"})

    # Phase 2: fresh registry from same path (= post-restart).
    registry_b = RunRegistry(persist_path=persist_path)
    restored = registry_b.get(entry.run_id)
    assert restored is not None
    assert restored.agent_name == "demo"
    assert restored.chain_id == "chain-A"
    assert restored.status == "input-required"
    assert restored.question == "Continue?"
    assert restored.history_events == [{"type": "phase", "name": "greet"}]


def test_restored_entry_has_task_none(tmp_path: Path) -> None:
    """Tier 2: restored ``RunEntry.task`` is ``None`` — the asyncio.Task
    was bound to the dead process and cannot be resurrected.
    Caller code that wants to re-spawn must do so explicitly.
    """
    persist_path = tmp_path / "run_registry.json"
    registry_a = RunRegistry(persist_path=persist_path)
    entry = registry_a.create(agent_name="demo", chain_id="chain-A")

    # Attach a fake task to simulate live-process state.
    loop = asyncio.new_event_loop()
    try:
        async def _noop() -> None:
            return None

        task = loop.create_task(_noop())
        registry_a.attach_task(entry.run_id, task)
        # Drive it to completion so it's not pending when we drop the loop.
        loop.run_until_complete(task)
    finally:
        loop.close()

    # Restart: restored entry must have task=None.
    registry_b = RunRegistry(persist_path=persist_path)
    restored = registry_b.get(entry.run_id)
    assert restored is not None
    assert restored.task is None


def test_restored_pending_intervention_has_fresh_future(tmp_path: Path) -> None:
    """Tier 2: when a ``pending_intervention`` was active at snapshot time,
    the restored entry's iv carries a **fresh future** (= the original
    future was volatile and bound to the dead awaiter).

    Verifies the from_dict path on UserIntervention preserves the iv
    metadata while reallocating the Future. Allows a future Gap 5
    Phase 2 re-bind step to attach a new awaiter.
    """
    persist_path = tmp_path / "run_registry.json"
    registry_a = RunRegistry(persist_path=persist_path)
    entry = registry_a.create(agent_name="demo", chain_id="chain-A")

    # Attach a pending intervention with a resolved-in-prior-process future
    # (= simulate "the prior process did set_result before crash").
    loop = asyncio.new_event_loop()
    try:
        async def _populate() -> None:
            iv = UserIntervention(
                kind="ask_user",
                prompt="What is your name?",
                detail="for greeting",
            )
            registry_a.update(
                entry.run_id,
                status="input-required",
                question=iv.prompt,
                pending_intervention=iv,
            )

        loop.run_until_complete(_populate())
    finally:
        loop.close()

    # Restart: restored iv must have a fresh, unresolved future.
    registry_b = RunRegistry(persist_path=persist_path)
    restored = registry_b.get(entry.run_id)
    assert restored is not None
    assert restored.pending_intervention is not None
    iv = restored.pending_intervention
    assert iv.kind == "ask_user"
    assert iv.prompt == "What is your name?"
    assert iv.detail == "for greeting"
    # Fresh future: not yet resolved.
    assert iv.future is not None
    assert not iv.future.done()


# ── 3. All mutations persist (= regression guards) ─────────────────────


def test_update_persists_status_change(tmp_path: Path) -> None:
    """Tier 2: ``update()`` writes a fresh snapshot reflecting the new state."""
    persist_path = tmp_path / "run_registry.json"
    registry = RunRegistry(persist_path=persist_path)
    entry = registry.create(agent_name="demo", chain_id="chain-A")

    registry.update(entry.run_id, status="completed", result="all done")
    data = json.loads(persist_path.read_text(encoding="utf-8"))
    assert data[entry.run_id]["status"] == "completed"
    assert data[entry.run_id]["result"] == "all done"


def test_append_event_persists_history(tmp_path: Path) -> None:
    """Tier 2: ``append_event()`` includes the new event in the snapshot."""
    persist_path = tmp_path / "run_registry.json"
    registry = RunRegistry(persist_path=persist_path)
    entry = registry.create(agent_name="demo", chain_id="chain-A")

    registry.append_event(entry.run_id, {"type": "phase", "name": "act"})
    registry.append_event(entry.run_id, {"type": "phase", "name": "decide"})

    data = json.loads(persist_path.read_text(encoding="utf-8"))
    assert data[entry.run_id]["history_events"] == [
        {"type": "phase", "name": "act"},
        {"type": "phase", "name": "decide"},
    ]


def test_cancel_persists_cancelled_status(tmp_path: Path) -> None:
    """Tier 2: ``cancel()`` snapshots the ``cancelled`` status."""
    persist_path = tmp_path / "run_registry.json"
    registry = RunRegistry(persist_path=persist_path)
    entry = registry.create(agent_name="demo", chain_id="chain-A")

    assert registry.cancel(entry.run_id) is True
    data = json.loads(persist_path.read_text(encoding="utf-8"))
    assert data[entry.run_id]["status"] == "cancelled"


def test_remove_drops_entry_from_snapshot(tmp_path: Path) -> None:
    """Tier 2: ``remove()`` rewrites the snapshot without the entry."""
    persist_path = tmp_path / "run_registry.json"
    registry = RunRegistry(persist_path=persist_path)
    entry_a = registry.create(agent_name="demo-a", chain_id="chain-A")
    entry_b = registry.create(agent_name="demo-b", chain_id="chain-B")

    registry.remove(entry_a.run_id)
    data = json.loads(persist_path.read_text(encoding="utf-8"))
    assert entry_a.run_id not in data
    assert entry_b.run_id in data


def test_answer_intervention_persists_cleared_state(tmp_path: Path) -> None:
    """Tier 2: ``answer_intervention()`` clears pending fields + persists."""
    persist_path = tmp_path / "run_registry.json"
    registry = RunRegistry(persist_path=persist_path)
    entry = registry.create(agent_name="demo", chain_id="chain-A")

    loop = asyncio.new_event_loop()
    try:
        async def _drive() -> None:
            iv = UserIntervention(kind="ask_user", prompt="?")
            registry.update(
                entry.run_id,
                status="input-required",
                question="?",
                pending_intervention=iv,
            )
            registry.answer_intervention(
                entry.run_id, InterventionAnswer(text="ok"),
            )

        loop.run_until_complete(_drive())
    finally:
        loop.close()

    data = json.loads(persist_path.read_text(encoding="utf-8"))
    assert data[entry.run_id]["status"] == "running"
    assert data[entry.run_id]["question"] is None
    assert "pending_intervention" not in data[entry.run_id]


# ── 4. Atomic write (= no partial file on crash mid-write) ──────────────


def test_persist_uses_atomic_rename(tmp_path: Path) -> None:
    """Tier 2: the snapshot writer goes via a ``.tmp`` file + atomic
    ``Path.replace()`` so a concurrent restore can't read a half-
    written file.

    Verified by checking the absence of stale ``.tmp`` files after a
    successful write (= replace removed the tmp).
    """
    persist_path = tmp_path / "run_registry.json"
    registry = RunRegistry(persist_path=persist_path)
    registry.create(agent_name="demo", chain_id="chain-A")

    assert persist_path.exists()
    # No leftover .tmp file.
    assert not persist_path.with_suffix(persist_path.suffix + ".tmp").exists()


# ── 5. Tolerant of missing / corrupt snapshot ──────────────────────────


def test_restore_from_missing_file_yields_empty_registry(tmp_path: Path) -> None:
    """Tier 2: construction with a non-existent ``persist_path`` leaves
    the registry empty (= no crash, fresh server starts cleanly).
    Subsequent mutations create the file.
    """
    persist_path = tmp_path / "does_not_exist.json"
    registry = RunRegistry(persist_path=persist_path)
    assert registry.list() == []
    assert not persist_path.exists()

    # First mutation should create the file.
    registry.create(agent_name="demo", chain_id="chain-A")
    assert persist_path.exists()


def test_restore_from_corrupt_json_yields_empty_registry(tmp_path: Path) -> None:
    """Tier 2: a corrupt snapshot (= invalid JSON) doesn't crash the
    server — registry starts empty + a warning is logged. Recovery
    path: any new mutation will overwrite with a fresh well-formed
    snapshot.
    """
    persist_path = tmp_path / "corrupt.json"
    persist_path.write_text("not valid json {{{", encoding="utf-8")

    registry = RunRegistry(persist_path=persist_path)
    assert registry.list() == []


def test_restore_from_non_dict_yields_empty_registry(tmp_path: Path) -> None:
    """Tier 2: a snapshot whose top-level JSON is not a dict (e.g. an
    array or string) is treated as corrupt — empty registry, no
    crash. Defensive against accidental file overwrites.
    """
    persist_path = tmp_path / "wrong_shape.json"
    persist_path.write_text("[1, 2, 3]", encoding="utf-8")

    registry = RunRegistry(persist_path=persist_path)
    assert registry.list() == []


def test_restore_skips_corrupt_entries_keeping_valid_ones(tmp_path: Path) -> None:
    """Tier 2: when one entry in the snapshot is malformed but others
    are well-formed, the valid ones still restore. Per-entry resilience
    avoids one corrupt iv data losing the whole registry.
    """
    persist_path = tmp_path / "partial_corrupt.json"
    persist_path.write_text(
        json.dumps({
            "good_run": {
                "run_id": "good_run",
                "agent_name": "demo",
                "chain_id": "chain-A",
                "status": "running",
                "history_events": [],
                "created_at": "2026-05-20T00:00:00+00:00",
                "updated_at": "2026-05-20T00:00:00+00:00",
            },
            "bad_run": "this should be a dict but is a string",
        }),
        encoding="utf-8",
    )

    registry = RunRegistry(persist_path=persist_path)
    assert registry.get("good_run") is not None
    assert registry.get("bad_run") is None
