"""Tier 2: #1418 — edit_file returns a show-not-judge context preview.

After a successful `edit_file`, the result carries a `preview` (additive to
`{status, replacements}`): a 1-based numbered-line view of the changed region so
the agent can SEE what landed and at what indentation. The guardrails are
behavioral, so the tests are too (real Workspace + op_runtime, no mocks):

- **show-not-judge**: the preview is numbered lines only — no syntax check, no
  validity verdict. A wrong-indent edit surfaces the real indent; a syntactically
  broken edit still yields a plain numbered view, never a judgment.
- **language-agnostic**: pure line slicing → works on a non-Python file.
- **bounded-by-construction**: a large multi-line insert is capped.
- edges: `replace_all` shows the first region (count stays in `replacements`);
  an empty `new_string` (deletion) shows the surrounding seam context.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.events.events import EventLog
from reyn.permissions.permissions import PermissionResolver
from reyn.tools import get_default_registry
from reyn.tools.dispatch import invoke_tool
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.workspace.workspace import Workspace


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={"file.read": "allow", "file.write": "allow"},
        project_root=tmp_path,
        interactive=False,
    )


def _ctx(tmp_path: Path) -> ToolContext:
    events = EventLog()
    ws = Workspace(events=events)
    return ToolContext(
        events=events,
        permission_resolver=_resolver(tmp_path),
        workspace=ws,
        caller_kind="router",
        router_state=RouterCallerState(),
    )


def _edit(tmp_path, args: dict) -> dict:
    return asyncio.run(invoke_tool(get_default_registry(), "edit_file", args, _ctx(tmp_path)))


# ── show-not-judge ──────────────────────────────────────────────────────────


def test_preview_shows_new_line_at_actual_indent_in_context(tmp_path, monkeypatch):
    """Tier 2: #1418 — a wrong-indent edit surfaces the new line at its ACTUAL
    indent inside the surrounding (still-indented) context, so the agent can
    self-notice the mismatch. The preview shows it; it never flags it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "m.py").write_text(
        "def f():\n        a = 1\n        b = 2\n        c = 3\n        return a\n"
    )
    result = _edit(
        tmp_path,
        {"path": "m.py", "old_string": "        b = 2", "new_string": "b = 2  # bad indent"},
    )
    assert result["status"] == "ok"
    preview = result["preview"]
    # The new line landed at indent 0 (line 3) ...
    assert "3\tb = 2  # bad indent" in preview
    # ... while the surrounding lines are still indent-8 — the contrast is visible.
    assert "        a = 1" in preview
    assert "        c = 3" in preview


def test_preview_has_no_validity_verdict(tmp_path, monkeypatch):
    """Tier 2: #1418 — show-not-judge: even a syntactically broken edit yields a
    plain numbered view (status stays ok — the op does not judge validity), with
    no verdict word in the preview."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "m.py").write_text("x = 1\ny = 2\nz = 3\n")
    result = _edit(
        tmp_path,
        {"path": "m.py", "old_string": "y = 2", "new_string": "y = (  "},  # broken
    )
    assert result["status"] == "ok"
    preview = result["preview"].lower()
    for verdict in ("error", "invalid", "syntax", "warning", "valid", "correct"):
        assert verdict not in preview, f"preview must not judge validity ({verdict!r} present)"


# ── language-agnostic ───────────────────────────────────────────────────────


def test_preview_works_on_non_python_file(tmp_path, monkeypatch):
    """Tier 2: #1418 — language-agnostic: a Markdown (non-Python) edit gets the
    same numbered-region view. No parser that could reject non-Python is run."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("# Title\n\nold paragraph\n\n## Section\n")
    result = _edit(
        tmp_path,
        {"path": "README.md", "old_string": "old paragraph", "new_string": "new paragraph"},
    )
    assert result["status"] == "ok"
    assert "3\tnew paragraph" in result["preview"]


# ── 1-based numbering ───────────────────────────────────────────────────────


def test_preview_line_numbers_are_1_based(tmp_path, monkeypatch):
    """Tier 2: #1418 — preview line numbers are 1-based and match the file's
    actual line positions (the changed line and its neighbors)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("L1\nL2\nL3\nL4\nL5\nL6\nL7\n")
    preview = _edit(tmp_path, {"path": "f.txt", "old_string": "L4", "new_string": "X4"})["preview"]
    assert "4\tX4" in preview
    assert "1\tL1" in preview
    assert "7\tL7" in preview


# ── edges ───────────────────────────────────────────────────────────────────


def test_preview_replace_all_shows_first_region_count_in_replacements(tmp_path, monkeypatch):
    """Tier 2: #1418 — replace_all shows the FIRST changed region in the preview;
    the total count stays in `replacements` (a far second occurrence is outside
    the first region's window)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("a\nTODO\nb\nc\nd\ne\nf\nTODO\ng\n")
    result = _edit(
        tmp_path,
        {"path": "f.txt", "old_string": "TODO", "new_string": "DONE", "replace_all": True},
    )
    assert result["status"] == "ok"
    assert result["replacements"] == 2
    preview = result["preview"]
    assert "2\tDONE" in preview        # first region shown
    assert "8\tDONE" not in preview    # far second occurrence not in the window


def test_preview_deletion_shows_surrounding_context(tmp_path, monkeypatch):
    """Tier 2: #1418 — an empty new_string (deletion) shows the surrounding
    context at the seam where the text was removed."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("one\ntwo\nDELETE_ME\nthree\nfour\n")
    result = _edit(tmp_path, {"path": "f.txt", "old_string": "DELETE_ME\n", "new_string": ""})
    assert result["status"] == "ok"
    preview = result["preview"]
    assert "DELETE_ME" not in preview  # the deleted text is gone
    assert "two" in preview            # surrounding lines frame the seam
    assert "three" in preview


def test_preview_multiline_insert_is_bounded(tmp_path, monkeypatch):
    """Tier 2: #1418 — bounded-by-construction: a large multi-line insert is
    truncated, not rendered in full, so the preview cannot bloat the result."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("head\nANCHOR\ntail\n")
    big = "\n".join(f"line{i}" for i in range(200))
    result = _edit(tmp_path, {"path": "f.txt", "old_string": "ANCHOR", "new_string": big})
    assert result["status"] == "ok"
    preview = result["preview"]
    assert "line0" in preview          # the start of the region is shown ...
    assert "line199" not in preview    # ... but the far tail is truncated, not full
    assert "preview truncated" in preview
