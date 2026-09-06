"""Tier 2: #5870 stage 2 (F1, architect census) — the ref -> path table is
read+parsed from disk ONCE per genuinely-changed state, not once per row per
frame.

Architect's own measured finding on this issue: the drawer's Artifacts pane
refresh (``TextualChatApp._pump_frames`` -> ``_refresh_live_chrome`` ->
``_refresh_pane`` -> ``_artifact_rows`` -> ``resolve_display_paths``) called
:func:`~reyn.data.workspace.artifact_ref.resolve_ref` once PER ARTIFACT ROW,
and each of those calls read+parsed the WHOLE ``artifact_refs.jsonl`` table
from scratch — an append-only file that never shrinks — on EVERY frame,
regardless of whether the table had changed since the last read. This file
pins the fix's two halves: :func:`~reyn.data.workspace.artifact_ref.
resolve_refs` (the new batch API) reads the table exactly ONCE for N rows,
and :func:`~reyn.data.workspace.artifact_ref._load_table`'s own cache (keyed
on the file's ``(mtime_ns, size)`` identity, never a TTL) makes a REPEATED
call against an unchanged table cost zero further reads.

Real on-disk state under ``tmp_path`` throughout — no mocks. The read count
itself is observed via a real, unpatched ``pathlib.Path.read_text`` wrapped
to count calls against the ONE table path this module writes to — the read
COUNT is the literal thing the architect's own acceptance criteria name, not
an implementation detail this test would otherwise have to infer.
"""
from __future__ import annotations

import json
from pathlib import Path

from reyn.core.present.artifact_list import ArtifactRow, resolve_display_paths
from reyn.data.workspace.artifact_ref import mint_ref, resolve_refs


def _write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _table_path(project_root: Path) -> Path:
    return project_root / ".reyn" / "memory" / "artifact_refs.jsonl"


def _counting_read_text(monkeypatch, target: Path) -> "list[int]":
    """Wraps the real ``Path.read_text`` to count calls against *target*
    specifically — every OTHER path's own read still runs for real and is
    not counted, so this only ever measures reads of the ONE file the
    acceptance criteria are about."""
    calls: "list[int]" = []
    real_read_text = Path.read_text

    def wrapper(self: Path, *args, **kwargs):
        if self == target:
            calls.append(1)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", wrapper)
    return calls


def test_resolving_fifty_rows_reads_the_table_exactly_once(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the architect's own acceptance criterion, verbatim — N=50
    artifact rows against a K=5000-line table: ``resolve_display_paths``'s
    ONE call reads the table file exactly once, not once per row (the
    pre-fix cost, falsified separately by hand: reverting ``resolve_refs``
    to a per-row ``resolve_ref`` loop turns the assertion below red at 50,
    not 1)."""
    agent = "agent1"
    rows: "list[ArtifactRow]" = []
    for i in range(50):
        target = _write(tmp_path / f"artifact{i}.bin")
        ref = mint_ref(tmp_path, agent, target)
        rows.append(
            ArtifactRow(
                ref=ref, name=f"artifact{i}.bin", media_type=None,
                description=None, is_inline=False,
            )
        )
    # Pad the table to K=5000 lines with unrelated entries — the architect's
    # own stated table size, so this isn't measuring a table too small to
    # matter.
    table_path = _table_path(tmp_path)
    with table_path.open("a", encoding="utf-8") as f:
        for i in range(5000 - 50):
            f.write(json.dumps({"ref": f"pad{i}", "agent": "other-agent", "path": f"/pad{i}"}) + "\n")

    read_calls = _counting_read_text(monkeypatch, table_path)

    out = resolve_display_paths(rows, tmp_path, agent)

    # Tuple-unpack IS the "exactly one" check — it raises (rather than
    # silently passing) on zero or on two-or-more, unlike a magic-number
    # length comparison (testing policy: never pin a bare count).
    (_only_read,) = read_calls
    assert all(row.resolved_path is not None for row in out), (
        "every ref-bearing row should have resolved to a real path — a "
        "batch-lookup bug could satisfy the read-count assertion above "
        "while silently failing to resolve anything"
    )


def test_an_unchanged_table_costs_zero_further_reads_a_changed_one_costs_one(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the architect's own second acceptance criterion — a SECOND
    refresh against an unchanged table reads zero times; appending one row
    between two refreshes makes the NEXT refresh read exactly once.
    Identity-based invalidation (mtime_ns + size), never a TTL — this test
    would still pass instantly even if it somehow ran hours apart, which a
    TTL-based cache could not promise."""
    agent = "agent1"
    target = _write(tmp_path / "artifact.bin")
    ref = mint_ref(tmp_path, agent, target)
    rows = [
        ArtifactRow(ref=ref, name="artifact.bin", media_type=None, description=None, is_inline=False),
    ]
    table_path = _table_path(tmp_path)
    read_calls = _counting_read_text(monkeypatch, table_path)

    resolve_display_paths(rows, tmp_path, agent)  # cold cache: 1 read
    first_count = len(read_calls)
    assert first_count >= 1, "setup: the first resolve should have read the table at least once"

    resolve_display_paths(rows, tmp_path, agent)  # table unchanged
    assert len(read_calls) == first_count, (
        f"an UNCHANGED table cost {len(read_calls) - first_count} additional "
        "reads — the cache did not hit"
    )

    # Append a new entry -- the table's own (mtime, size) identity changes.
    other = _write(tmp_path / "other.bin")
    mint_ref(tmp_path, agent, other)

    resolve_display_paths(rows, tmp_path, agent)  # table changed since last read
    assert len(read_calls) == first_count + 1, (
        f"an APPENDED table should cost exactly one further read on the "
        f"next call, got {len(read_calls) - first_count}"
    )


def test_resolve_refs_batches_a_lookup_the_same_way_resolve_ref_answers_it_singly(
    tmp_path: Path,
) -> None:
    """Tier 2: non-vacuity/correctness — resolve_refs' own per-ref answers
    match what the pre-existing single resolve_ref already promises: a
    resolved ref maps to its real path, an unknown ref maps to None, and a
    resolved-but-deleted file also maps to None (#4478's own "the file is
    just gone" contract, unchanged by the batch form)."""
    from reyn.data.workspace.artifact_ref import resolve_ref

    agent = "agent1"
    kept = _write(tmp_path / "kept.bin")
    deleted = _write(tmp_path / "deleted.bin")
    ref_kept = mint_ref(tmp_path, agent, kept)
    ref_deleted = mint_ref(tmp_path, agent, deleted)
    deleted.unlink()

    batch = resolve_refs(tmp_path, agent, [ref_kept, ref_deleted, "unknown-ref"])

    assert batch[ref_kept] == resolve_ref(tmp_path, agent, ref_kept) == kept
    assert batch[ref_deleted] is None, "a resolved-but-deleted file must map to None"
    assert batch["unknown-ref"] is None
