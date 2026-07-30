"""Tier 2: interactive chat router's `read_file` alias signals a self-bounded
truncation instead of silently dropping it (#3191).

``op_runtime/file.py``'s `read` handler sets `status: "truncated"` + a
decision-enabling `note` (chars shown of total, on-disk path, resume offset)
when the inline cap self-bounds a read. #3191's defect was that the interactive
chat path never saw it: `RouterLoop._normalise_router_tool_result` flattened
`read_file` to a bare `result["content"]` string BEFORE `to_canonical` ever ran
(same choke point #2998/#3190 found for `list_directory`/`glob`), so the LLM saw
truncated text with no signal that anything was cut. #3191 fixed it by appending
the note to that flat string.

**#3429 deleted the flattening** — it existed for byte-identity with a
pre-ADR-0026 router branch, which is back-compat, and it was ALSO where the two
spellings of one read diverged (it keyed on the DISPATCHED name, so the
qualified route skipped it). The chat path now goes through `file_to_canonical`
like every other consumer, and `note` was added to `_file_signal_meta`'s
whitelist so the resume information rides the frontmatter every consumer reads.
The signal is therefore delivered by a different mechanism; the requirement is
unchanged and is what these witnesses assert.

Three witnesses, each real-file-backed (no mocks, no fakes):
  1. Consumer reach: the LLM-visible `role: tool` body — canonical + offload +
     render, the real assembly — carries the op layer's own note when the op
     layer actually truncated. Asserts the signal is IN what the LLM reads, not
     just set upstream on a dict nobody downstream reads (the #2998/#3190
     failure).
  2. Control: an untruncated read carries no note (a "make truncated always
     True" non-fix would also pass witness 1 without this control).
  3. End-to-end via the real `READ_FILE` handler + a real inline-cap-sized
     file, confirming the op layer's `status`/`note` fields actually exist on a
     genuine result (not just the synthetic dict fed to witness 1).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.offload.canonical import to_canonical
from reyn.core.offload.seam import build_offload_body, render_tool_result
from reyn.data.workspace.workspace import Workspace
from reyn.tools.file import READ_FILE
from reyn.tools.types import ToolContext


def _llm_visible(name: str, result: dict) -> str:
    """The `role: tool` body the LLM actually reads for ``result``.

    The real assembly, not a paraphrase of it: ``to_canonical`` (dispatching on
    the invoked identity) → ``build_offload_body`` (signal meta → frontmatter,
    structured attachments inline) → ``render_tool_result``.

    Building the default registry first is load-bearing, not incidental: a
    tool's canonical mapper is declared when the tool registers, so without it
    ``to_canonical`` takes the lossless whole-dict fallback and this helper
    would measure the fallback rather than the mapper."""
    from reyn.tools import get_default_registry

    get_default_registry()
    canonical = to_canonical(result, source=name)
    frontmatter, text, _media, _ct = build_offload_body(canonical, save_fn=None)
    return render_tool_result(frontmatter, text)


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


def test_llm_visible_read_result_carries_the_truncation_note():
    """Tier 2: the truncation signal reaches what the LLM reads.

    Asserted on the assembled `role: tool` body, so it covers the whole
    delivery path rather than any one stage of it — which is the point: #3429
    replaced the stage that used to carry the note, and the requirement is that
    the note still arrives."""
    result = {
        "kind": "file", "op": "read", "path": "big.txt", "status": "truncated",
        "content": "line one\nline two\n",
        "note": (
            "content truncated to fit context (19 of 5000 chars shown); the "
            "full file is on disk at 'big.txt' — re-read from offset 2 to continue."
        ),
        "next_offset": 2, "total_chars": 5000,
    }
    out = _llm_visible("read_file", result)

    assert result["content"] in out, "the file body itself must still reach the LLM"
    assert "truncated" in out, "the LLM is not told the read was cut short"
    assert "19 of 5000 chars shown" in out
    assert "re-read from offset 2" in out


def test_llm_visible_read_result_carries_no_note_when_not_truncated():
    """Tier 2: control — an untruncated (`status: "ok"`) read carries no
    truncation note and no resume instruction. A "always append the note"
    non-fix would fail this and make the signal meaningless."""
    result = {
        "kind": "file", "op": "read", "path": "small.txt", "status": "ok",
        "content": "whole file\n",
    }
    out = _llm_visible("read_file", result)

    assert "whole file" in out
    assert "truncated" not in out
    assert "re-read from offset" not in out


# ── 2. End-to-end: real handler + a real inline-cap-sized file ────────────────


def test_read_file_handler_truncated_status_and_note_are_real(tmp_path, monkeypatch):
    """Tier 2: the op_runtime `read` handler genuinely sets `status: "truncated"`
    + a `note` on a real oversized file (not just a synthetic dict as in witness
    1 above) — confirms the field names the delivery path reads actually exist on
    a live result, and that a live result's note reaches the LLM-visible body."""
    big = "x" * 200_000 + "\n"
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    ctx = _make_ctx(tmp_path, monkeypatch)

    result = _run(READ_FILE.handler({"path": "big.txt"}, ctx))

    assert result.get("status") == "truncated"
    assert result.get("note")
    assert len(result["content"]) < len(big)

    out = _llm_visible("read_file", result)
    assert result["content"] in out
    # The note reaches the LLM through the frontmatter, where YAML may line-wrap
    # a long value — so assert the decision-enabling FACTS it carries rather than
    # the byte string (which would be a formatting pin, not a behavioural one).
    assert "truncated" in out
    assert str(result["total_chars"]) in out
    assert "re-read from offset" in out
