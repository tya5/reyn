"""Tier 1: #4494 design C — ``rows_from_ref_table_entries``, projecting the
durable artifact-ref table's own raw ``{"ref", "path"}`` entries into
``ArtifactRow`` objects — the fallback source a client with no live
conversation state (a remote client, or a local one right after a
restart) uses to populate the Artifacts pane.
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.present.artifact_list import ArtifactRow, rows_from_ref_table_entries


def test_projects_ref_and_basename_with_no_media_type_or_description():
    """Tier 1: the ref table only ever records (ref, path) — this function
    must not fabricate a media_type/description it has no source for."""
    entries = [{"ref": "abc123", "path": "/project/reports/q1.pdf"}]

    rows = rows_from_ref_table_entries(entries)

    assert rows == [
        ArtifactRow(
            ref="abc123",
            name="q1.pdf",
            media_type=None,
            description=None,
            is_inline=False,
        )
    ]


def test_preserves_input_order():
    """Tier 1: mirrors ``list_refs_for_agent``'s own newest-first ordering
    — this function does no re-sorting of its own, so the caller's order
    is what ends up on screen."""
    entries = [
        {"ref": "r2", "path": "/p/b.pdf"},
        {"ref": "r1", "path": "/p/a.pdf"},
    ]

    rows = rows_from_ref_table_entries(entries)

    assert [r.ref for r in rows] == ["r2", "r1"]


def test_skips_a_malformed_entry_missing_a_required_key():
    """Tier 1: (accept-side) a shape the ref table itself would never
    produce (guards against a caller handing this function something
    else) is dropped rather than raising."""
    entries = [{"ref": "abc"}, {"path": "/p/x.pdf"}, {"ref": "ok", "path": "/p/y.pdf"}]

    rows = rows_from_ref_table_entries(entries)

    assert [r.ref for r in rows] == ["ok"]


def test_empty_entries_returns_empty_rows():
    """Tier 1: (accept-side) no entries -> no rows, not a crash."""
    assert rows_from_ref_table_entries([]) == []


def test_name_is_the_path_basename_not_the_full_path(tmp_path):
    """Tier 1: mirrors ``artifact_row_label``'s own reason for preferring
    ``resolved_path`` over a bare ``name`` — this function's ``name`` is
    ONLY the basename, matching how a ref-backed live-conversation row's
    own ``name`` field already behaves before ``resolve_display_paths``
    fills in the real path."""
    entries = [{"ref": "r", "path": str(Path(tmp_path) / "sub" / "dir" / "file.docx")}]

    rows = rows_from_ref_table_entries(entries)

    assert rows[0].name == "file.docx"
