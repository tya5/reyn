"""Tier 2: #5896 stage ② — two of the architect's three ruled items only
(https://github.com/tya5/reyn/issues/5896#issuecomment-5562613871 and the
lead's follow-up quoting the architect's 2026-09-06 22:42 comment):

  ① a one-time, idempotent migration that gives every existing
    history-content file a real spill-manifest line
    (:func:`reyn.data.workspace.media_store.migrate_history_content_manifest`);
  ② the default flip for a file the manifest has NO line for at all —
    "不明なら守る" ("unknown ⇒ protect"), the OPPOSITE of the pre-stage-②
    default ("unknown, evictable").

Item ③ (GC eviction order — "spilled 先行に") is EXPLICITLY NOT this PR's
subject: the architect's 2026-09-06 22:42 comment folds "un-spilled も退避可"
into stage ③, "owner 確認" pending — see the lead's own retraction on this
issue. Stage ① (#5896 stage ① proper — un-spilled is an outright eviction
exclusion, #5901) is unchanged and already covered by
``test_5896_history_content_ref.py``; this file adds only the two items
above, never re-asserting stage ①'s own behaviour.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from reyn.data.workspace.media_store import (
    MediaStore,
    MediaStoreConfig,
    migrate_history_content_manifest,
    spill_manifest_path_for,
)


def _bump_all_mtimes_forward(directory: Path) -> None:
    """Mirrors ``test_5896_history_content_ref.py``'s own helper — push
    every file already in *directory* one second into the past so
    "oldest" is unambiguous inside a fast test loop."""
    for path in directory.rglob("*"):
        if path.is_file():
            st = path.stat()
            os.utime(path, (st.st_atime, st.st_mtime - 1))


_CAP_WARNING = "cannot be met"


def _cap_warnings(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelname == "WARNING" and _CAP_WARNING in r.getMessage()
    ]


def test_a_file_with_no_manifest_line_is_never_an_eviction_candidate(
    tmp_path: Path, caplog,
) -> None:
    """Tier 2: item ② — "unknown ⇒ protect". A file physically present
    under this session's history-content directory that the manifest has
    NO line for at all (simulating the #5364 §1.4 crash-race: the content
    write landed, the deferred manifest-append job never ran) survives an
    eviction pass even when it is the OLDEST file and the cap is set so
    low that evicting every KNOWN candidate still leaves the tree over
    cap — the pass ends over cap, warns once, and never touches the
    unknown file. Public seam: ``MediaStore.is_known_file``.

    Strip-falsify: drop the ``is_known_file`` skip in
    ``_evict_history_content_over_cap`` → the unknown file (oldest) is
    the one evicted → red."""
    caplog.set_level(logging.WARNING, logger="reyn.data.workspace.media_store")
    config = MediaStoreConfig(history_content_max_bytes=1)
    store = MediaStore(config, project_root=tmp_path, agent_name="alice", session_id="main")

    unknown_dir = store.history_content_dir
    unknown_dir.mkdir(parents=True, exist_ok=True)
    unknown_file = unknown_dir / "unmigrated.txt"
    unknown_file.write_bytes(b"x" * 5)
    _bump_all_mtimes_forward(unknown_dir)
    assert not store.is_known_file(str(unknown_file.relative_to(tmp_path))), (
        "test setup sanity: the manifest genuinely has no line for this file"
    )

    # A real, spilled write — the cap (1 byte) forces an eviction pass the
    # instant this lands; the ONLY known candidate is this file itself.
    store.save_tool_result("payload number 0 " * 2, seq=0)

    assert unknown_file.exists(), (
        "a file with no manifest line must never be an eviction candidate, "
        "regardless of age or how over-cap the pass ends"
    )
    (warning,) = _cap_warnings(caplog)
    assert "no manifest line" in warning and "1 file(s)" in warning, warning


def test_cross_session_gc_never_lists_a_neighbours_unmigrated_file(
    tmp_path: Path,
) -> None:
    """Tier 2: the same item ② condition on the PROJECT-wide pass (#5366's
    ``storage.max_bytes``), in the SAME construction-order shape that
    broke stage ①'s un-spilled exclusion (architect co-vet 🔴 #2 on
    #5901): bob's store is constructed FIRST; alice's unmigrated file is
    placed on disk AFTER — so bob's construction-time view cannot know
    about it — and bob's project-wide preview must still not list it: the
    ``known`` set is derived from the tree at pass time
    (``MediaStore._manifest_paths_project_wide``), not from the caller's
    own in-memory view.

    Strip-falsify: pass ``self._history_content_spill_paths`` (the
    caller's own set) instead of the tree-derived ``known`` →
    bob lists alice's unmigrated file → red."""
    from reyn.config.infra import StorageConfig

    bob = MediaStore(
        project_root=tmp_path, agent_name="bob", session_id="main",
        storage=StorageConfig(max_bytes=10),
    )
    alice = MediaStore(project_root=tmp_path, agent_name="alice", session_id="main")
    unknown_dir = alice.history_content_dir
    unknown_dir.mkdir(parents=True, exist_ok=True)
    unknown_file = unknown_dir / "unmigrated.txt"
    unknown_file.write_bytes(b"x" * 5)
    _bump_all_mtimes_forward(unknown_dir)
    assert not bob.is_known_file(str(unknown_file.relative_to(tmp_path))), (
        "test setup sanity: bob's construction-time view genuinely does not know this file"
    )

    assert bob.cross_session_eviction_preview() == []

    evictable = alice.save_tool_result("payload number 1 " * 2, seq=1)
    preview = bob.cross_session_eviction_preview()
    assert (tmp_path / evictable["path"]).resolve() in {p.resolve() for p in preview}
    assert unknown_file.resolve() not in {p.resolve() for p in preview}


def test_migrate_history_content_manifest_backfills_from_history_truth_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """Tier 2: item ① — the one-time migration. Simulates the manifest-loss
    disaster the architect's closing #5901 comment names (both a
    previously-spilled and a previously-un-spilled file losing their line
    at once): after the manifest file is deleted, an injected
    ``iter_content_refs`` (standing in for
    ``router_history_buffer.iter_history_content_refs``'s real
    ``history.jsonl`` census, #5896 stage ② item ①'s own layering — the
    data-layer migration function never imports the runtime-layer wire
    vocabulary directly) supplies the TRUTH for two files; a third file
    ("orphan" — no row anywhere ever named it, e.g. dropped by rewind)
    gets no truth at all and defaults un-spilled ("不明なら守る").

    Idempotency witness (PR body requirement — "2 回目が no-op であること"):
    a second call, same inputs, migrates and protects zero.

    Strip-falsify: drop the ``known`` exclusion in the migration's own
    scan (re-migrate an already-tracked path) → the spilled file's TRUE
    entry gets silently re-defaulted on a second run → red (the second
    call's ``migrated`` would be nonzero)."""
    store = MediaStore(project_root=tmp_path, agent_name="alice", session_id="main")
    spilled_block = store.save_tool_result("payload number 0 " * 2, seq=0)  # spilled=True default
    unspilled_block = store.save_tool_result("payload number 1 " * 2, seq=1, spilled=False)

    manifest_path = spill_manifest_path_for(tmp_path)
    assert manifest_path.is_file(), "test setup: both real writes already have a normal manifest line"

    # Simulate the manifest-loss disaster: BOTH lines are gone at once.
    manifest_path.unlink()

    orphan_dir = store.history_content_dir
    orphan = orphan_dir / "orphan.txt"
    orphan.write_text("nobody's history.jsonl row references me")
    orphan_rel = str(orphan.relative_to(tmp_path))

    def _fake_iter_content_refs(_project_root: Path) -> list[tuple[str, bool]]:
        return [
            (spilled_block["path"], True),
            (unspilled_block["path"], False),
        ]

    result = migrate_history_content_manifest(
        tmp_path, iter_content_refs=_fake_iter_content_refs,
    )
    assert result == {"migrated": 3, "protected_unknown": 2}, result

    reopened = MediaStore(project_root=tmp_path, agent_name="alice", session_id="main")
    assert reopened.is_known_file(spilled_block["path"])
    assert reopened.is_known_file(unspilled_block["path"])
    assert reopened.is_known_file(orphan_rel), "the orphan must get a line too — just a protective one"
    assert not reopened.is_unspilled_file(spilled_block["path"]), (
        "the TRUE fact (spilled) must survive the migration, not default"
    )
    assert reopened.is_unspilled_file(unspilled_block["path"]), (
        "the TRUE fact (un-spilled) must survive the migration"
    )
    assert reopened.is_unspilled_file(orphan_rel), (
        "no row named the orphan — '不明なら守る' defaults it un-spilled, never spilled"
    )

    second = migrate_history_content_manifest(
        tmp_path, iter_content_refs=_fake_iter_content_refs,
    )
    assert second == {"migrated": 0, "protected_unknown": 0}, (
        "a second run over the same tree must be a true no-op — this IS the "
        "'一度きり' witness the PR body requires"
    )


def test_migrate_history_content_manifest_is_a_noop_with_no_history_content_tree(
    tmp_path: Path,
) -> None:
    """Tier 2: a project with no ``history-content/`` tree at all (nothing
    ever written) migrates zero, not an error — the function's own
    documented degrade."""
    result = migrate_history_content_manifest(tmp_path, iter_content_refs=lambda _p: [])
    assert result == {"migrated": 0, "protected_unknown": 0}


def test_an_unmigrated_files_protection_survives_a_real_wal_truncation_and_reconstruct(
    tmp_path: Path,
) -> None:
    """Tier 2: CLAUDE.md's recovery-feature truncate-falsify gate, applied
    to #5896 stage ②'s new invariant — "unknown ⇒ protect" — the way
    ``test_4584_persist_tier_survives_wal_truncation.py`` already applies
    it to this same manifest for the sibling #4584 claim. Set X (a file
    with no manifest line is protected from eviction) → run the REAL
    production WAL-truncation primitive (heavy enough to drop every event
    this test wrote) across it → open a BRAND-NEW ``StateLog`` (the
    "reconstruct" step, same primitive ``test_2946_item1_state_log_tail_
    scan.py`` uses) → open a BRAND-NEW ``MediaStore`` over the SAME
    project (a later process attaching here) → X must still hold: the
    unmigrated file is STILL protected, because the manifest (and this
    file's very absence from it) was never derived from the WAL in the
    first place — the same "moved somewhere that survives" claim #4584's
    own test makes for this exact manifest, now applied to stage ②'s new
    default rather than stage ①'s old one.

    Strip-falsify: this test alone cannot distinguish "genuinely survives"
    from "the WAL was never touched" — that is exactly WHY it drives a
    real ``truncate_below`` over real WAL entries this test itself wrote
    (mirrors #4584's own module docstring on this point) rather than
    asserting a bare path never changed."""
    import asyncio

    from reyn.core.events.state_log import StateLog

    project_root = tmp_path
    config = MediaStoreConfig(history_content_max_bytes=1)
    store = MediaStore(config, project_root=project_root, agent_name="alice", session_id="main")

    unknown_dir = store.history_content_dir
    unknown_dir.mkdir(parents=True, exist_ok=True)
    unknown_file = unknown_dir / "unmigrated.txt"
    unknown_file.write_bytes(b"x" * 5)
    _bump_all_mtimes_forward(unknown_dir)
    store.save_tool_result("payload number 0 " * 2, seq=0)  # forces the cap-1 eviction pass
    assert unknown_file.exists(), "test setup: X holds before any WAL activity at all"

    wal_path = project_root / ".reyn" / "state" / "wal.jsonl"
    log = StateLog(wal_path)
    seen: dict[str, str] = {}

    async def _churn_and_truncate() -> None:
        for i in range(5):
            await log.append("inbox_put", target=f"a{i}", payload={})
        await log.flush()
        seen["before"] = wal_path.read_text(encoding="utf-8")
        await log.truncate_below(1_000_000)
        await log.flush()

    asyncio.run(_churn_and_truncate())
    assert seen["before"].count('"inbox_put"') == 5, "test setup: the appends must have landed"
    assert log.last_truncate_stats["dropped"] >= 1, (
        "test setup: truncate_below dropped nothing — the assertion below would "
        "then measure an untruncated WAL while claiming otherwise"
    )
    StateLog(wal_path)  # the reconstruct step: a fresh process attaching here

    reopened = MediaStore(config, project_root=project_root, agent_name="alice", session_id="main")
    assert unknown_file.exists(), (
        "X (an unmigrated file's protection from eviction) must survive a WAL "
        "truncation it was never derived from"
    )
    assert not reopened.is_known_file(str(unknown_file.relative_to(project_root))), (
        "and it must still read as UNKNOWN after reconstruct — a real spilled "
        "write forcing eviction again must still leave it alone"
    )
    reopened.save_tool_result("payload number 1 " * 2, seq=1)
    assert unknown_file.exists(), (
        "post-reconstruct: a fresh eviction pass still must not select the "
        "unknown file"
    )
