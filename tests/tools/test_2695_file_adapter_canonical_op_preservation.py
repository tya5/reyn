"""Tier 2b: #2695 — file_* tool adapters preserve the ``op`` dispatch field.

``file_to_canonical`` (the FP-0056 canonical mapper the file_* ToolDefinitions
declare) sub-dispatches on the result's ``op`` field to pick a per-op rendering
(glob → the match paths, read → the content, …). The ``glob_files`` and
``list_directory`` adapters used to "normalize" their ok result to a
caller-ergonomic ``{pattern, matches, count}`` / ``{path, entries}`` dict that
DROPPED ``op`` (and ``status``). With ``op`` absent every branch was skipped and
the mapper fell through to ``f"{op}: {status}"`` = the literal ``"None: ok"`` —
silent total loss of the match list to the LLM (#2695, part of the #2688 sweep).

This is a shape-preservation contract at the tool-adapter → canonical-mapper
boundary: every file_* adapter's ok result must carry the ``op`` the mapper
dispatches on, so the matches/entries actually render.

Real registry / Workspace / op_runtime — no mocks. Assertions are on the public
canonical text and the public result ``op`` field (the dispatch contract), never
on formatting or private state.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.offload.canonical import file_to_canonical
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionResolver
from reyn.tools import get_default_registry
from reyn.tools.dispatch import invoke_tool
from reyn.tools.types import RouterCallerState, ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    events = EventLog()
    return ToolContext(
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={"file.read": "allow", "file.write": "allow"},
            project_root=tmp_path,
            interactive=False,
        ),
        workspace=Workspace(events=events, base_dir=tmp_path),
        caller_kind="router",
        router_state=RouterCallerState(),
    )


def _invoke(tmp_path: Path, name: str, args: dict) -> dict:
    return asyncio.run(invoke_tool(get_default_registry(), name, args, _ctx(tmp_path)))


def test_glob_result_renders_matches_not_none_ok(tmp_path, monkeypatch):
    """Tier 2b: glob_files' canonical output carries the matched paths, not "None: ok".

    #2955/#2972: the match LIST now rides in the ``structured`` attachment (a
    genuinely-structured record-list result, same shape as ``web_search`` —
    needed so a pipeline ``for_each`` can fan out over it without a shell
    round-trip), with ``text`` reduced to a short count summary. Assert the
    paths are NOT lost (still present, just relocated) and the #2695
    silent-loss symptom ("None: ok") stays fixed.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "alpha.txt").write_text("a\n")
    (tmp_path / "beta.txt").write_text("b\n")

    result = _invoke(tmp_path, "glob_files", {"pattern": "*.txt"})
    # The dispatch field the canonical mapper needs must survive normalization.
    assert result.get("op") == "glob"

    canonical = file_to_canonical(result)
    text = canonical["text"]
    assert text != "None: ok"  # the #2695 silent-loss symptom
    assert "None" not in text
    assert text == "2 files"

    structured_matches = [
        att["data"] for att in canonical["attachments"] if att.get("kind") == "structured"
    ][0]
    assert set(structured_matches) == {"alpha.txt", "beta.txt"}


def test_glob_empty_renders_no_matches_not_none_ok(tmp_path, monkeypatch):
    """Tier 2b: an empty glob renders a readable no-match body, not "None: ok"."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "only.md").write_text("x\n")

    result = _invoke(tmp_path, "glob_files", {"pattern": "*.txt"})
    assert result.get("op") == "glob"
    canonical = file_to_canonical(result)
    text = canonical["text"]
    assert text != "None: ok"
    assert "no matches" in text.lower()
    # Empty structured data is still an EXPLICIT attachment (not omitted) — the
    # canonical_degraded invariant's "structured attachment present" branch
    # covers this, mirroring web_search_to_canonical's data:[] convention.
    assert canonical["attachments"] == [{"kind": "structured", "data": []}]


def test_list_directory_renders_entries_not_none_ok(tmp_path, monkeypatch):
    """Tier 2b: list_directory's canonical output carries the entries, not "None: ok"."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "one.txt").write_text("1\n")
    (tmp_path / "two.txt").write_text("2\n")

    result = _invoke(tmp_path, "list_directory", {"path": "."})
    assert result.get("op") == "glob"  # list_directory delegates to the glob op

    canonical = file_to_canonical(result)
    text = canonical["text"]
    assert text != "None: ok"
    assert "None" not in text
    assert text == "2 files"

    structured_matches = [
        att["data"] for att in canonical["attachments"] if att.get("kind") == "structured"
    ][0]
    assert set(structured_matches) == {"one.txt", "two.txt"}


def test_every_file_adapter_ok_result_carries_op(tmp_path, monkeypatch):
    """Tier 2b: guard — every file_* adapter's ok result carries a non-None ``op``.

    ``file_to_canonical`` dispatches on ``op``; an adapter that drops it silently
    routes to the ``"None: ok"`` fallback (the #2695 class). This drives all seven
    real adapters and asserts the dispatch field is present, so the class cannot
    silently regress via a new/edited adapter that re-normalizes ``op`` away."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src.txt").write_text("hello world\n")
    (tmp_path / "gone.txt").write_text("bye\n")

    ok_calls = [
        ("read_file", {"path": "src.txt"}),
        ("grep_files", {"pattern": "hello"}),
        ("glob_files", {"pattern": "*.txt"}),
        ("list_directory", {"path": "."}),
        ("write_file", {"path": "new.txt", "content": "x"}),
        ("edit_file", {"path": "src.txt", "old_string": "hello", "new_string": "hi"}),
        ("delete_file", {"path": "gone.txt"}),
    ]
    for name, args in ok_calls:
        result = _invoke(tmp_path, name, args)
        assert result.get("op") is not None, f"{name} ok-result dropped the op dispatch field"
        # And the canonical rendering never degrades to the None-dispatch fallback.
        assert file_to_canonical(result)["text"] != "None: ok", name
