"""Tier 2: #4584 — two tables moved from ``.reyn/cache/`` (DERIVED,
"rebuilt after restore") to ``.reyn/memory/`` (PERSIST): ``artifact_ref.py``'s
(agent, path) -> ref table (#4482 PR-1) and ``media_store.py``'s tool-result
spill-provenance manifest (#4381/#4432).

Both tables share the SAME defect that motivated the move: the data they
hold exists ONLY at the instant it is written — no WAL event, no
conversation-log entry, nothing else durable carries it (measured directly
in #4494/#4584) — while ``cache/``'s own doc classification promised an
operator it was safe to delete because it would be "rebuilt after restore".
Filing genuinely-primary data under a "derived, safe to clean up" tier is
what let a correctly-following operator (or a future GC policy) destroy it.

This file's job is the CLAUDE.md recovery-feature PR gate, adapted to what
this PR actually does: not "add reconstruction, prove it survives WAL
truncation" (there is no reconstruction here — that is the whole point),
but "prove the table is now genuinely OUTSIDE the WAL-truncation-affected
tier" — a real ``StateLog.truncate_below`` call, heavy enough to drop
every WAL entry, followed by a real re-open of a fresh ``StateLog`` over
the SAME project (the standard "reconstruct" step
``test_2946_item1_state_log_tail_scan.py`` uses for the identical
primitive), with each table's own round trip (mint/save -> truncate ->
resolve/is_spill) still returning the SAME path throughout — "moved" and
"moved somewhere that survives" are different claims, and only a real
round trip through the actual persisted files proves the second one (a
bare path-string diff would go green either way).

Real on-disk state under ``tmp_path``/real ``StateLog``/real ``MediaStore``
throughout — no mocks.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.data.workspace.artifact_ref import mint_ref, resolve_ref
from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig


def _write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _churn_and_truncate_wal(wal_path: Path) -> None:
    """Append a handful of WAL entries, then truncate past every one of
    them — the heaviest truncation short of deleting the file — then
    re-open a brand-new ``StateLog`` over the result (the "reconstruct"
    step: a fresh process attaching to this project)."""
    log = StateLog(wal_path)
    seen: dict[str, str] = {}

    async def _go() -> None:
        for i in range(5):
            await log.append("inbox_put", target=f"a{i}", payload={})
        await log.flush()
        seen["before"] = wal_path.read_text(encoding="utf-8")
        await log.truncate_below(1_000_000)
        await log.flush()
        seen["after"] = wal_path.read_text(encoding="utf-8")

    asyncio.run(_go())
    # Positive control on the SOURCE events, not on a counter that survives
    # either way: `current_seq` is >= 1 the moment five appends land, so
    # asserting it says nothing about whether anything was removed (#2839's
    # own shape — assert the source entries before, and their absence after).
    #
    # The "after" side reads the primitive's OWN report (`last_truncate_stats`,
    # the public surface #3180's own truncate-falsify already uses) rather than
    # counting lines ourselves. Counting works but measures a proxy: an
    # external guess at what the rewrite did, which also has to encode the
    # clamp `_do_truncate` applies ("never drop the highest seq present, even
    # if min_keep_seq exceeds it" — so a truncate-past-everything over a single
    # kind always leaves one entry, and "zero remain" would fail against
    # correct behaviour). Asking the mechanism how many it dropped needs none
    # of that.
    assert seen["before"].count('"inbox_put"') == 5, "test setup: the appends must have landed"
    assert log.last_truncate_stats["dropped"] >= 1, (
        "test setup: truncate_below dropped nothing — every assertion below "
        "would then be measuring an untruncated WAL while claiming otherwise"
    )
    StateLog(wal_path)  # the reconstruct step: a fresh process attaching here


def test_both_tables_live_directly_under_memory_never_under_cache_or_state(
    tmp_path: Path,
) -> None:
    """Tier 2: structural witness for WHY truncation cannot touch either
    table — neither path falls under ``.reyn/state/`` (the WAL) or
    ``.reyn/config/`` (the write-gated config-generation registries), and
    neither falls under ``.reyn/cache/`` (the DERIVED tier this PR moves
    them OUT of) either."""
    target = _write(tmp_path / "report.pptx")
    mint_ref(tmp_path, "alice", target)
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    store.save_tool_result("big output", chain_id="c1", tool="exec", seq=1)

    reyn_root = tmp_path / ".reyn"
    ref_table = reyn_root / "memory" / "artifact_refs.jsonl"
    spill_manifest = reyn_root / "memory" / "tool_result_spills.jsonl"
    assert ref_table.is_file()
    assert spill_manifest.is_file()
    for table_path in (ref_table, spill_manifest):
        assert table_path.parent == reyn_root / "memory"
        assert (reyn_root / "state") not in table_path.parents
        assert (reyn_root / "config") not in table_path.parents
        assert (reyn_root / "cache") not in table_path.parents, (
            f"{table_path} must no longer live under the DERIVED cache/ tier "
            "(#4584's own fix)"
        )


def test_falsify_artifact_ref_survives_a_real_wal_truncation_and_reconstruct(
    tmp_path: Path,
) -> None:
    """Tier 2: LOAD-BEARING falsification (artifact_ref.py's half) — mint a
    ref, run the REAL production WAL-truncation primitive across every
    event this test wrote, re-open a brand-new ``StateLog`` (the
    reconstruct step), and confirm ``resolve_ref`` still returns the SAME
    path throughout. The ref was never written to the WAL in the first
    place, so truncating it has zero effect BY CONSTRUCTION — this test
    witnesses that construction directly rather than assuming it."""
    target = _write(tmp_path / "report.pptx")
    ref = mint_ref(tmp_path, "alice", target)
    assert resolve_ref(tmp_path, "alice", ref) == target.resolve()  # test premise

    _churn_and_truncate_wal(tmp_path / ".reyn" / "state" / "wal.jsonl")

    resolved = resolve_ref(tmp_path, "alice", ref)
    assert resolved == target.resolve(), (
        "the artifact ref was lost across a WAL truncation it was never "
        "derived from. This does NOT witness #4584 itself (WAL truncation "
        "never touched cache/, so this passed before the move too) — it "
        "guards the FUTURE change of deriving the table from the WAL"
    )


def test_falsify_spill_manifest_survives_a_real_wal_truncation_and_reconstruct(
    tmp_path: Path,
) -> None:
    """Tier 2: LOAD-BEARING falsification (media_store.py's half) — save a
    tool-result spill, run the SAME real WAL-truncation + reconstruct
    cycle, then open a BRAND-NEW ``MediaStore`` (mirrors
    ``test_prune_drops_a_manifest_entry_whose_file_was_deleted_out_of_band``'s
    own "a fresh construction = a later process" pattern) and confirm
    ``is_tool_result_spill`` still recognizes the SAME path."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    block = store.save_tool_result("big output", chain_id="c1", tool="exec", seq=1)
    assert store.is_tool_result_spill(block["path"])  # test premise

    _churn_and_truncate_wal(tmp_path / ".reyn" / "state" / "wal.jsonl")

    reopened = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    assert reopened.is_tool_result_spill(block["path"]), (
        "the spill-manifest entry was lost across a WAL truncation it was "
        "never derived from. Same scope as the sibling above: not a witness "
        "for #4584, a guard against deriving this manifest from the WAL"
    )


# ── the falsification #4584 actually needs: the danger is cache REMOVAL ──


def _wipe_cache(project_root: Path) -> None:
    """Delete ``.reyn/cache/`` outright — what an operator does when the
    layout doc says a tier is safe to delete, and what ``index_drop`` does
    to a subtree of it from inside a running agent. This, not WAL
    truncation, is the event #4584 moved the two tables away from."""
    cache = project_root / ".reyn" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(cache)
    assert not cache.exists(), "test setup: the cache tier must really be gone"


def test_falsify_artifact_ref_survives_the_cache_tier_being_deleted(
    tmp_path: Path,
) -> None:
    """Tier 2: LOAD-BEARING falsification — mint a ref, delete the whole
    ``.reyn/cache/`` tier, and resolve it again.

    This is the one that FLIPS: before #4584 the table lived in
    ``.reyn/cache/artifact_refs.jsonl``, so wiping that tier took the
    ref with it and ``resolve_ref`` returned ``None``. The sibling
    WAL-truncation tests above pass either way — WAL truncation never
    touched ``cache/`` — so they cannot witness this move, which is why
    this test exists separately rather than as a variation of them.
    """
    target = _write(tmp_path / "report.pptx")
    ref = mint_ref(tmp_path, "alice", target)
    assert resolve_ref(tmp_path, "alice", ref) == target.resolve()  # test premise

    _wipe_cache(tmp_path)

    assert resolve_ref(tmp_path, "alice", ref) == target.resolve(), (
        "the artifact ref died when the cache tier was deleted — the table is "
        "back under a tier the layout doc says is safe to delete, and every "
        "previously minted /open link is now unresolvable (#4584)"
    )


def test_falsify_spill_manifest_survives_the_cache_tier_being_deleted(
    tmp_path: Path,
) -> None:
    """Tier 2: LOAD-BEARING falsification, media_store.py's half — same
    flip: the manifest used to live under ``.reyn/cache/`` and a wipe of
    that tier made every recorded spill unrecognizable to a later
    process."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    block = store.save_tool_result("big output", chain_id="c1", tool="exec", seq=1)
    assert store.is_tool_result_spill(block["path"])  # test premise

    _wipe_cache(tmp_path)

    reopened = MediaStore(MediaStoreConfig(), project_root=tmp_path)
    assert reopened.is_tool_result_spill(block["path"]), (
        "the spill-manifest entry died when the cache tier was deleted — the "
        "manifest is back under the tier the layout doc says is safe to "
        "delete (#4584)"
    )
