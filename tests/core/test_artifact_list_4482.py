"""Tier 1: #4482 PR-3 — deriving the "open an artifact" list from resolved
present nodes (`core/present/artifact_list.py`), pure functions only.

Fixtures are shaped like real `artifact_payload.py` (#4505/PR-2) output —
`{"component": "artifact", "media_type": ..., "name": ..., "body": {...}}`
— not hand-waved shortcuts, so a schema drift in the payload builder would
show up here as a shape these tests stop recognizing.
"""
from __future__ import annotations

from reyn.core.present.artifact_list import (
    ArtifactRow,
    collect_artifact_rows,
    stat_row,
)


def _ref_node(name: str, ref: str, *, media_type: str = "text/html", description=None) -> dict:
    node = {
        "component": "artifact", "media_type": media_type, "name": name,
        "body": {"ref": ref, "size": 123},
    }
    if description is not None:
        node["description"] = description
    return node


def _inline_node(name: str, *, media_type: str = "text/plain") -> dict:
    return {
        "component": "artifact", "media_type": media_type, "name": name,
        "body": {"inline": "hello"},
    }


def test_collects_a_reference_backed_artifact():
    """Tier 1: a source-backed (ref) artifact node becomes an openable row."""
    rows = collect_artifact_rows([[_ref_node("report.pptx", "abc123")]])
    assert rows == [ArtifactRow(
        ref="abc123", name="report.pptx", media_type="text/html",
        description=None, is_inline=False,
    )]


def test_inline_artifact_has_no_ref_and_is_marked_inline():
    """Tier 1: an inline (no-real-file) artifact has nothing for the OS to
    open — ref is None, is_inline is True, distinguishable from a
    reference-backed row."""
    rows = collect_artifact_rows([[_inline_node("snippet.txt")]])
    assert rows[0].ref is None
    assert rows[0].is_inline is True


def test_soft_binding_miss_produces_no_row():
    """Tier 1: `{"component": "artifact"}` alone (present's existing
    soft-skip shape for an unresolved binding) yields nothing displayable
    yet — not a crash, not a blank row."""
    rows = collect_artifact_rows([[{"component": "artifact"}]])
    assert rows == []


def test_error_marker_still_produces_a_row_naming_the_failure():
    """Tier 1: a `source_not_found` error marker (apply_artifact_resolution's
    own shape for a missing file) is still LISTED — distinguishable from a
    healthy row (error set, ref None) so the user sees why it can't open,
    rather than the row silently vanishing."""
    node = {"component": "artifact", "error": "source_not_found"}
    rows = collect_artifact_rows([[node]])
    assert rows[0].error == "source_not_found"
    assert rows[0].ref is None


def test_non_artifact_nodes_are_ignored():
    """Tier 1: a present node from any other component (image/text/table/…)
    never contributes a row — this list is artifact-only."""
    rows = collect_artifact_rows([[{"component": "text", "text": "hi"}]])
    assert rows == []


def test_order_is_newest_first_across_messages():
    """Tier 1: the list reads newest-first — `node_lists` is walked in the
    caller's own (oldest-to-newest) message order and REVERSED, so the
    caller (FlowView entries, oldest-first) needs no pre-sorting of its
    own."""
    rows = collect_artifact_rows([
        [_ref_node("first.pptx", "ref-1")],
        [_ref_node("second.pptx", "ref-2")],
        [_ref_node("third.pptx", "ref-3")],
    ])
    assert [r.name for r in rows] == ["third.pptx", "second.pptx", "first.pptx"]


def test_order_is_newest_first_within_one_message_too():
    """Tier 1: multiple artifact nodes in the SAME message (e.g. an agent
    presenting two files in one turn) also come out newest-first — the
    later node in that message's own node list is "newer" within the turn."""
    rows = collect_artifact_rows([
        [_ref_node("a.pptx", "ref-a"), _ref_node("b.pptx", "ref-b")],
    ])
    assert [r.name for r in rows] == ["b.pptx", "a.pptx"]


def test_empty_input_yields_empty_list():
    """Tier 1: no messages, or messages with no nodes at all, is a no-op —
    not an error."""
    assert collect_artifact_rows([]) == []
    assert collect_artifact_rows([[], []]) == []


def test_a_malformed_node_entry_is_skipped_not_fatal():
    """Tier 1: a non-dict entry in a node list (should never happen given
    the catalog's own structural gate, but this module doesn't trust its
    caller blindly) is skipped rather than raising."""
    rows = collect_artifact_rows([["not-a-dict", _ref_node("ok.pptx", "ref-ok")]])
    assert [r.name for r in rows] == ["ok.pptx"]


# ── stat_row ──────────────────────────────────────────────────────────────


def test_stat_row_returns_none_for_a_none_path():
    """Tier 1: no resolved path (e.g. a deleted or unresolvable ref) — no
    I/O attempted, None returned."""
    assert stat_row(None) is None


def test_stat_row_returns_the_real_size(tmp_path):
    """Tier 1: a real file's current size, read via a single stat() call —
    this test is the one place in this file real filesystem I/O happens,
    matching the module's own claim that stat_row is its ONLY I/O."""
    p = tmp_path / "real.pptx"
    p.write_bytes(b"x" * 42)
    assert stat_row(p) == 42


def test_stat_row_returns_none_for_a_missing_file(tmp_path):
    """Tier 1: a path that resolved once but no longer exists (deleted
    out from under the list — #4478's GC domain) fails gracefully."""
    ghost = tmp_path / "gone.pptx"
    assert stat_row(ghost) is None
