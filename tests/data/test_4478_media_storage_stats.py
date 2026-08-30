"""Tier 2: #4478 Phase 1 — ``MediaStore.storage_stats`` (policy-independent
on-disk footprint measurement) and the spill-manifest self-prune.

Both are measurement/hygiene only — neither deletes an artifact under
``media_dir``/``tool_results_dir``. See ``media_store.py``'s own module
docstring for why any actual eviction policy (TTL/max-N) stays out of
scope until real measurement evidence exists.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig


def _store(tmp_path: Path, agent_name: "str | None" = "test-agent") -> MediaStore:
    return MediaStore(MediaStoreConfig(), project_root=tmp_path, agent_name=agent_name, session_id="test-session")


# ── storage_stats ─────────────────────────────────────────────────────────


def test_storage_stats_reports_zero_on_a_fresh_project(tmp_path):
    """Tier 2: neither directory exists yet — stats report all-zero, not
    an error."""
    stats = _store(tmp_path).storage_stats()
    assert stats.media_file_count == 0
    assert stats.media_bytes == 0
    assert stats.tool_result_file_count == 0
    assert stats.tool_result_bytes == 0


def test_storage_stats_counts_files_and_bytes_written_through_the_real_api(tmp_path):
    """Tier 2: writes via save_media/save_tool_result — the real production
    call paths, not hand-placed files — and confirms storage_stats reflects
    them exactly."""
    store = _store(tmp_path)
    img_a = b"\x89PNG" + b"\x00" * 100
    img_b = b"\x89PNG" + b"\x00" * 50
    store.save_media(img_a, mime_type="image/png", chain_id="c1", tool="web_fetch", seq=1)
    store.save_media(img_b, mime_type="image/png", chain_id="c1", tool="web_fetch", seq=2)
    store.save_tool_result("hello world", chain_id="c1", tool="exec", seq=1)

    stats = store.storage_stats()
    assert stats.media_file_count == 2
    assert stats.media_bytes == len(img_a) + len(img_b)
    assert stats.tool_result_file_count == 1
    assert stats.tool_result_bytes == len(b"hello world")


def test_storage_stats_never_deletes_or_writes_anything(tmp_path):
    """Tier 2: (accept-side) calling storage_stats repeatedly does not
    change the directory contents — this is a read, not a side-effecting
    scan."""
    store = _store(tmp_path)
    store.save_media(b"x" * 10, mime_type="image/png", chain_id="c", tool="t", seq=1)
    before = sorted(p.name for p in store.media_dir.iterdir())

    store.storage_stats()
    store.storage_stats()

    after = sorted(p.name for p in store.media_dir.iterdir())
    assert after == before


# ── spill-manifest self-prune (#4478 condition ②: prunes the LEDGER only) ──


def _manifest_path(tmp_path: Path) -> Path:
    return tmp_path / ".reyn" / "memory" / "tool_result_spills.jsonl"  # #4584: moved out of cache/


def test_prune_drops_a_manifest_entry_whose_file_was_deleted_out_of_band(tmp_path):
    """Tier 2: a spill written in an earlier process, then deleted by
    something outside MediaStore (the documented "user/operator deletes
    out-of-band" lifecycle) — the NEXT construction's manifest load drops
    the now-stale entry rather than carrying it forever."""
    store = _store(tmp_path)
    block = store.save_tool_result("big output", chain_id="c1", tool="exec", seq=1)
    spilled_path = tmp_path / block["path"]
    assert spilled_path.exists()

    # Out-of-band deletion — NOT via MediaStore.
    spilled_path.unlink()

    # A fresh construction (= a later process) reloads the manifest.
    reopened = _store(tmp_path)
    assert not reopened.is_history_content_spill(block["path"])

    # The manifest ON DISK was rewritten to drop the stale line too — a
    # yet-later construction doesn't need to re-discover the same staleness.
    manifest_lines = _manifest_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert manifest_lines == []


def test_prune_keeps_a_manifest_entry_whose_file_still_exists(tmp_path):
    """Tier 2: (accept-side) the prune must not touch a live entry — this
    is the mirror of the drop test above."""
    store = _store(tmp_path)
    block = store.save_tool_result("still here", chain_id="c1", tool="exec", seq=1)

    reopened = _store(tmp_path)
    assert reopened.is_history_content_spill(block["path"])
    manifest_paths = {
        json.loads(line)["path"]
        for line in _manifest_path(tmp_path).read_text(encoding="utf-8").splitlines()
    }
    assert manifest_paths == {str((tmp_path / block["path"]).resolve())}


def test_prune_never_deletes_the_referenced_artifact_itself(tmp_path):
    """Tier 2: #4478 condition ② — the prune rewrites the MANIFEST (the
    ledger under .reyn/memory/, #4584: moved from .reyn/cache/), never a
    file under tool_results_dir. This
    is the falsifiable form of "prune deletes zero bytes of anyone's
    actual content": construct a mixed manifest (one live entry, one
    entry a test writes by hand pointing at a path that never existed)
    and confirm the live artifact survives the prune untouched."""
    store = _store(tmp_path)
    block = store.save_tool_result("keep me", chain_id="c1", tool="exec", seq=1)
    live_path = tmp_path / block["path"]

    # Hand-append a manifest line for a path that was never written —
    # simulates a manifest entry surviving from an artifact deleted
    # before this test's own store even started.
    manifest = _manifest_path(tmp_path)
    ghost = tmp_path / ".reyn" / "tool-results" / "ghost.txt"
    with manifest.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"path": str(ghost.resolve())}) + "\n")

    _store(tmp_path)  # triggers the prune

    assert live_path.exists()
    assert live_path.read_text(encoding="utf-8") == "keep me"
