"""Tier 1: #4482 PR-3 — deriving the "open an artifact" list from resolved
present nodes (`core/present/artifact_list.py`).

`collect_artifact_rows` is pure; `resolve_display_paths`/`stat_row` are the
two functions that do real (isolated, caller-gated) I/O — see the module's
own docstring for why that split exists.

Fixtures are shaped like real `artifact_payload.py` (#4505/PR-2) output —
`{"component": "artifact", "media_type": ..., "name": ..., "body": {...}}`
— not hand-waved shortcuts, so a schema drift in the payload builder would
show up here as a shape these tests stop recognizing.
"""
from __future__ import annotations

from reyn.core.present.artifact_list import (
    ArtifactRow,
    collect_artifact_rows,
    resolve_display_paths,
    stat_row,
)
from reyn.data.workspace.artifact_ref import mint_ref


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


def test_a_ref_carrying_inline_preview_is_still_treated_as_openable():
    """Tier 1: #4574 design C — a small source-backed file's body carries
    BOTH `ref` AND an `inline` preview (never inline-ONLY, as of #4574).
    This must still resolve to an openable (`is_inline=False`) row, not a
    `(inline — already shown above)` one — the exact label-truthfulness
    fix #4574 required: before it, a small source-backed file got
    `is_inline=True` with NO ref, so the Art tab's row both lied about
    "already shown above" (nothing was shown — the body renderer had no
    `artifact` branch at all) AND had nothing to open."""
    node = {
        "component": "artifact", "media_type": "text/html", "name": "report.html",
        "body": {"ref": "abc123", "size": 20, "inline": "<h1>hi</h1>"},
    }
    rows = collect_artifact_rows([[node]])
    assert rows == [ArtifactRow(
        ref="abc123", name="report.html", media_type="text/html",
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


# ── resolve_display_paths ────────────────────────────────────────────────


def test_resolve_display_paths_sets_a_project_relative_path(tmp_path):
    """Tier 1: #4482 PR-3 review fix — a resolvable ref gets a real,
    project-root-relative `resolved_path`, via the SAME `resolve_ref`
    the open path itself calls (real mint, real file, real resolve — no
    hand-built fixture standing in for the resolution)."""
    sub = tmp_path / "reports"
    sub.mkdir()
    target = sub / "q1.pptx"
    target.write_text("bytes")
    ref = mint_ref(tmp_path, "default", target)

    row = ArtifactRow(ref=ref, name="q1.pptx", media_type=None, description=None, is_inline=False)
    resolved = resolve_display_paths([row], tmp_path, "default")
    assert resolved[0].resolved_path == "reports/q1.pptx"


def test_resolve_display_paths_disambiguates_same_named_files(tmp_path):
    """Tier 1: the exact failure mode the review named — two files with
    the SAME basename in different directories must resolve to
    DIFFERENT display paths, not the same ambiguous name twice."""
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "report.pptx").write_text("a")
    (dir_b / "report.pptx").write_text("b")
    ref_a = mint_ref(tmp_path, "default", dir_a / "report.pptx")
    ref_b = mint_ref(tmp_path, "default", dir_b / "report.pptx")

    rows = [
        ArtifactRow(ref=ref_a, name="report.pptx", media_type=None, description=None, is_inline=False),
        ArtifactRow(ref=ref_b, name="report.pptx", media_type=None, description=None, is_inline=False),
    ]
    resolved = resolve_display_paths(rows, tmp_path, "default")
    paths = {r.resolved_path for r in resolved}
    assert paths == {"a/report.pptx", "b/report.pptx"}


def test_resolve_display_paths_leaves_an_unresolvable_ref_alone(tmp_path):
    """Tier 1: an unknown/deleted ref's `resolved_path` stays `None` — the
    display layer falls back to `name`, the best available answer when
    there is genuinely nothing to resolve."""
    row = ArtifactRow(
        ref="unknown-ref", name="ghost.pptx", media_type=None, description=None, is_inline=False,
    )
    resolved = resolve_display_paths([row], tmp_path, "default")
    assert resolved[0].resolved_path is None


def test_resolve_display_paths_leaves_an_inline_row_alone(tmp_path):
    """Tier 1: an inline row (`ref is None`) has nothing to resolve — no
    resolve_ref call, `resolved_path` stays `None`."""
    row = ArtifactRow(ref=None, name="(inline)", media_type=None, description=None, is_inline=True)
    resolved = resolve_display_paths([row], tmp_path, "default")
    assert resolved[0].resolved_path is None
