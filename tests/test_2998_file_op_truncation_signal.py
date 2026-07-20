"""Tier 2: glob / list_directory signal a max_results cap that discarded matches (#2998).

Before this fix, ``_file_signal_meta`` (``canonical.py``) only carried ``op`` / ``status`` /
``path`` — a glob/list_directory result silently capped at the (intentional, unchanged) 50-match
default gave the LLM no way to distinguish "this is everything" from "this is the first 50 of
more". ``read`` already had a precedent (``status: "truncated"``), but glob/list_directory had
none.

Four witnesses, each real-file-backed (no mocks, no fakes):
  1. glob: cap hit -> ``truncated`` fires (+ counts).
  2. glob: cap NOT hit -> ``truncated`` absent (the control — without it, "always true" would
     also go green).
  3. list_directory: same pair (its own op_runtime dispatch — #2695 established list_directory
     is a DISTINCT silent-loss surface from glob, not automatically covered by fixing glob).
  4. Consumer reach: (a) ``file_to_canonical``'s glob branch puts the signal in LLM-visible
     frontmatter meta (the pipeline/CodeAct/direct-glob_files consumer path); (b) the
     interactive chat router's ``list_directory`` alias bypasses ``to_canonical`` entirely
     (``RouterLoop._normalise_router_tool_result`` flattens to a bare list BEFORE canonicalization
     — verified by reading router_loop.py, not assumed) so it carries the signal through its own
     trailing-note append instead; this test asserts the note actually lands in the returned
     value, not just that ``result["truncated"]`` was set upstream (the "loaded but nobody reads
     it" failure mode the brief warns about).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.offload.canonical import file_to_canonical
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.router_loop import RouterLoop
from reyn.tools.file import GLOB_FILES, LIST_DIRECTORY
from reyn.tools.types import ToolContext


def _make_ctx(tmp_path: Path, monkeypatch) -> ToolContext:
    monkeypatch.chdir(tmp_path)
    ws = Workspace(events=EventLog())
    ws.base_dir = tmp_path
    return ToolContext(
        caller_kind="router", events=EventLog(), permission_resolver=None, workspace=ws,
    )


def _run(coro):
    return asyncio.run(coro)


def _write_n_files(tmp_path: Path, n: int, prefix: str = "file") -> None:
    for i in range(n):
        (tmp_path / f"{prefix}_{i:03d}.txt").write_text("x\n", encoding="utf-8")


# ── 1/2. glob_files: witness + control ───────────────────────────────────────


def test_glob_files_truncated_when_cap_discards_matches(tmp_path, monkeypatch):
    """Tier 2: 60 real files, default (50) max_results -> truncated=True + counts."""
    _write_n_files(tmp_path, 60)
    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(GLOB_FILES.handler({"pattern": "*.txt"}, ctx))

    assert result.get("status") == "ok"
    assert len(result.get("matches", [])) == 50
    assert result.get("truncated") is True
    assert result.get("total_count") == 60
    assert result.get("returned_count") == 50


def test_glob_files_no_truncated_flag_when_cap_not_hit(tmp_path, monkeypatch):
    """Tier 2: control — 5 real files, default max_results -> no truncated key at all.

    Without this control, a "make truncated always True" non-fix would also pass
    test 1 — this is what catches that."""
    _write_n_files(tmp_path, 5)
    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(GLOB_FILES.handler({"pattern": "*.txt"}, ctx))

    assert result.get("status") == "ok"
    assert len(result.get("matches", [])) == 5
    assert "truncated" not in result
    assert "total_count" not in result


# ── 3. list_directory: same pair, its own op_runtime dispatch (#2695 sibling) ──


def test_list_directory_truncated_when_cap_discards_entries(tmp_path, monkeypatch):
    """Tier 2: list_directory's own adapter (tools/file.py::_handle_list) forwards
    the glob op's truncation signal — #2695 established list_directory is a
    DISTINCT code path from glob_files (its own field-copy in `_handle_list`), so
    fixing glob alone does not automatically cover it."""
    _write_n_files(tmp_path, 60)
    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(LIST_DIRECTORY.handler({"path": "."}, ctx))

    assert result.get("status") == "ok"
    assert len(result.get("matches", [])) == 50
    assert result.get("truncated") is True
    assert result.get("total_count") == 60
    assert result.get("returned_count") == 50


def test_list_directory_no_truncated_flag_when_cap_not_hit(tmp_path, monkeypatch):
    """Tier 2: control for list_directory."""
    _write_n_files(tmp_path, 5)
    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(LIST_DIRECTORY.handler({"path": "."}, ctx))

    assert result.get("status") == "ok"
    assert len(result.get("matches", [])) == 5
    assert "truncated" not in result


# ── 4a. Consumer reach: file_to_canonical's glob branch -> LLM-visible meta ────


def test_file_to_canonical_glob_meta_carries_truncated(tmp_path, monkeypatch):
    """Tier 2: the pipeline/CodeAct/direct-glob_files consumer path — `to_canonical`
    -> `build_offload_body` renders `meta` as frontmatter verbatim (seam.py: "Signal
    meta goes to the frontmatter as-is"), so asserting it lands in `meta` here IS
    asserting it reaches that consumer's LLM-visible text, not just an internal dict."""
    _write_n_files(tmp_path, 60)
    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(GLOB_FILES.handler({"pattern": "*.txt"}, ctx))
    # GLOB_FILES.handler's raw op_runtime result already carries op="glob" (the
    # file_to_canonical inner discriminator) — no envelope unwrap needed here.

    canonical = file_to_canonical(result)
    assert canonical["meta"].get("truncated") is True
    assert canonical["meta"].get("total_count") == 60
    assert canonical["meta"].get("returned_count") == 50


def test_file_to_canonical_glob_meta_omits_truncated_when_absent(tmp_path, monkeypatch):
    """Tier 2: control for the canonical-meta consumer path."""
    _write_n_files(tmp_path, 5)
    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(GLOB_FILES.handler({"pattern": "*.txt"}, ctx))

    canonical = file_to_canonical(result)
    assert "truncated" not in canonical["meta"]


# ── 4b. Consumer reach: interactive chat router's list_directory alias ────────


def test_normalise_router_tool_result_list_directory_appends_truncation_note():
    """Tier 2: `RouterLoop._normalise_router_tool_result` is the exact choke point
    that flattens a dict result to a bare list BEFORE `to_canonical` ever runs for
    `list_directory` in the interactive chat tool-calling loop (verified by reading
    router_loop.py: `_invoke_via_registry` calls this, and its return value is `r`
    fed straight into the content_str builder that only calls `to_canonical` when
    `r` is still a dict). Asserting the note is IN the returned list is asserting
    the signal survives past this choke point to what the LLM actually sees —
    catching "loaded truncated onto meta but this path drops meta entirely"."""
    result = {
        "op": "glob", "status": "ok", "path": ".",
        "entries": [f"e{i}.txt" for i in range(50)],
        "matches": [f"e{i}.txt" for i in range(50)],
        "truncated": True, "total_count": 60, "returned_count": 50,
    }
    out = RouterLoop._normalise_router_tool_result("list_directory", result)

    assert isinstance(out, list)
    # All 50 real entries survive untouched, PLUS a trailing note naming both
    # counts — the decision-enabling fact (how many of how many), not just a
    # boolean. Checking membership + note content (not len()) so this stays a
    # behavioral assertion, not a pinned shape.
    assert all(e in out for e in result["entries"])
    note_candidates = [e for e in out if e not in result["entries"]]
    assert note_candidates, "expected a trailing truncation note appended to entries"
    note = note_candidates[0]
    assert "50" in note and "60" in note


def test_normalise_router_tool_result_list_directory_no_note_when_not_truncated():
    """Tier 2: control — an untruncated result passes through unchanged (byte-
    identical to the pre-#2998 shape — no spurious note, no shape change)."""
    result = {
        "op": "glob", "status": "ok", "path": ".",
        "entries": ["a.txt", "b.txt"], "matches": ["a.txt", "b.txt"],
    }
    out = RouterLoop._normalise_router_tool_result("list_directory", result)

    assert out == ["a.txt", "b.txt"]
