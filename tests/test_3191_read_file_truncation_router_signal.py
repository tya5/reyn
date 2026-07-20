"""Tier 2: interactive chat router's `read_file` alias signals a self-bounded
truncation instead of silently dropping it (#3191).

``op_runtime/file.py``'s `read` handler already sets `status: "truncated"` +
a decision-enabling `note` (chars shown of total, on-disk path, resume offset)
when the inline cap self-bounds a read — this ALREADY reaches the pipeline/
CodeAct consumer path via `file_to_canonical`'s `_file_signal_meta` (`status`
is in its whitelist). But `RouterLoop._normalise_router_tool_result` flattens
`read_file` to a bare `result["content"]` string BEFORE `to_canonical` ever
runs for the interactive chat tool-calling loop (same choke point #2998/#3190
already found for `list_directory`/`glob`) — so the LLM in that path saw only
the truncated text with no signal at all that anything was cut.

Three witnesses, each real-file-backed (no mocks, no fakes):
  1. Consumer reach: the router choke point (`_normalise_router_tool_result`)
     appends the op layer's own `note` when the op layer actually truncated —
     asserts the signal is IN the string the LLM sees, not just set upstream
     on a dict nobody downstream reads (the failure #2998/#3190 named).
  2. Control: an untruncated read passes through byte-identical to the pre-fix
     shape — no spurious note (a "make truncated always True" non-fix would
     also pass witness 1 without this control).
  3. End-to-end via the real `READ_FILE` handler + a real inline-cap-sized
     file, confirming the op layer's `status`/`note` fields the router branch
     reads actually exist on a genuine result (not just the synthetic dict
     fed to witness 1).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.router_loop import RouterLoop
from reyn.tools.file import READ_FILE
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


# ── 1. Consumer reach: interactive chat router's read_file alias ──────────────


def test_normalise_router_tool_result_read_file_appends_truncation_note():
    """Tier 2: `RouterLoop._normalise_router_tool_result` is the exact choke point
    that flattens a dict result to `result["content"]` BEFORE `to_canonical` ever
    runs for `read_file` in the interactive chat tool-calling loop (verified by
    reading router_loop.py: `_invoke_via_registry` calls this, and its return
    value feeds straight into the content_str builder that only calls
    `to_canonical` when the value is still a dict). Asserting the note is IN the
    returned string is asserting the signal survives past this choke point to
    what the LLM actually sees."""
    result = {
        "kind": "file", "op": "read", "path": "big.txt", "status": "truncated",
        "content": "line one\nline two\n",
        "note": (
            "content truncated to fit context (19 of 5000 chars shown); the "
            "full file is on disk at 'big.txt' — re-read from offset 2 to continue."
        ),
        "next_offset": 2, "total_chars": 5000,
    }
    out = RouterLoop._normalise_router_tool_result("read_file", result)

    assert isinstance(out, str)
    assert out.startswith(result["content"])
    assert "19 of 5000 chars shown" in out
    assert "re-read from offset 2" in out


def test_normalise_router_tool_result_read_file_no_note_when_not_truncated():
    """Tier 2: control — an untruncated (`status: "ok"`) read passes through
    byte-identical to the pre-#3191 shape (bare content, no note appended). A
    "always append the note" non-fix would fail this and make the signal
    meaningless."""
    result = {
        "kind": "file", "op": "read", "path": "small.txt", "status": "ok",
        "content": "whole file\n",
    }
    out = RouterLoop._normalise_router_tool_result("read_file", result)

    assert out == "whole file\n"


# ── 2. End-to-end: real handler + a real inline-cap-sized file ────────────────


def test_read_file_handler_truncated_status_and_note_are_real(tmp_path, monkeypatch):
    """Tier 2: the op_runtime `read` handler genuinely sets `status: "truncated"`
    + a `note` on a real oversized file (not just a synthetic dict as in witness
    1 above) — confirms the field names the router branch reads actually exist
    on a live result, and that the router branch (fed this exact shape) also
    appends the note."""
    big = "x" * 200_000 + "\n"
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    ctx = _make_ctx(tmp_path, monkeypatch)

    result = _run(READ_FILE.handler({"path": "big.txt"}, ctx))

    assert result.get("status") == "truncated"
    assert result.get("note")
    assert len(result["content"]) < len(big)

    out = RouterLoop._normalise_router_tool_result("read_file", result)
    assert isinstance(out, str)
    assert out.startswith(result["content"])
    assert result["note"] in out
