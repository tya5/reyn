"""Tier 2: #4381 — ``next_char_offset`` actually works end-to-end, and
re-reading a tool-result SPILL file whole errors with a prescriptive
remedy instead of silently chaining another spill.

Owner report: the resume value ``op_runtime/file.py`` computes and
returns (``next_char_offset``, for a single line longer than the model's
context window) had no way BACK IN — ``tools/file.py``'s router-facing
schema declared only ``path``/``offset``/``limit``, and its handler never
read ``args.get("char_offset")`` even though ``FileIROp`` already
supported the field. An LLM that received ``next_char_offset`` had no
parameter to pass it back through — the resume was permanently
unreachable from the tool surface, even though the op layer itself always
worked correctly (``test_read_single_line_char_truncation_2335.py``
already covers THAT half).

Separately: a bare, unbounded re-read of a file that is ITSELF the
output of a previous tool-result SPILL (owner-ratified term — "入らない
から出す。不可避", NOT "offload" — see ``MediaStore.is_tool_result_
spill``'s own docstring) can come back oversized under ``op_runtime``'s
own window-derived cap, get truncated, and STILL be too big for the
router's separate, independent token-derived spill trigger
(``services/tool_result_cap.py``) — spilling it AGAIN. This is a real,
unbounded chain (traced before writing this fix, not assumed), broken by
detecting the spill-path re-read and erroring with the exact remedy
("specify char_offset and re-read") instead of truncating into the same
loop. The provenance registry is PERSISTED (lead-coder review round 2):
a reference to a spill path can outlive the process that wrote it (it
can sit in ``history.jsonl``, replayed as a plain ``read_file`` after a
restart) — an in-memory-only registry would silently stop recognizing a
real spill the moment the process that wrote it exits, reopening the
exact loop this file exists to close. Verified directly against a
SECOND, independently-constructed ``MediaStore`` instance, not by
inspecting the first one's own state.

Real ``OpContext`` + real files + a real ``MediaStore`` (no fakes) — the
same construction ``test_read_single_line_char_truncation_2335.py``
already established for this module.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES as CAP
from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.workspace.media_store import MediaStore
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl


def _ctx(tmp_path: Path, *, media_store: "MediaStore | None" = None) -> OpContext:
    ev = EventLog()
    return OpContext(
        workspace=Workspace(events=ev, base_dir=tmp_path),
        events=ev, permission_decl=PermissionDecl(), actor="t",
        media_store=media_store,
    )


def _read(tmp_path: Path, ctx: OpContext, **kw) -> dict:
    return asyncio.run(handle(FileIROp(kind="file", op="read", **kw), ctx))


# ── ① char_offset reaches the op layer through the router tool schema ──────


@pytest.mark.asyncio
async def test_handle_read_forwards_char_offset_to_file_i_r_op(tmp_path: Path) -> None:
    """Tier 2: the router-facing adapter (``tools/file.py::_handle_read``)
    reads ``args["char_offset"]`` and sets it on the ``FileIROp`` it
    builds — the exact gap this issue reports (the field existed on
    ``FileIROp`` and in ``op_runtime`` already; only this adapter/schema
    seam dropped it). Verified via an OBSERVABLE difference in the
    returned content, not by inspecting the constructed op internally.

    ``huge_line`` is sized ``CAP + 5000`` (same shape
    ``test_single_line_over_cap_is_char_truncated_and_honest`` already
    uses) so exactly ONE resume completes the tail — a much larger line
    would need a round-trip loop (covered separately by
    ``test_single_line_tail_round_trips_via_char_offset``), which is not
    this test's own concern (the ADAPTER forwarding one field, not the
    op layer's own paging correctness)."""
    from reyn.tools.file import _handle_read
    from reyn.tools.types import ToolContext

    huge_line = "y" * (CAP + 5000)  # one line, no newline
    (tmp_path / "wide.txt").write_text(huge_line)

    ev = EventLog()
    tool_ctx = ToolContext(
        events=ev,
        permission_resolver=None,
        workspace=Workspace(events=ev, base_dir=tmp_path),
        caller_kind="router",
    )
    first = await _handle_read({"path": "wide.txt"}, tool_ctx)
    assert first["status"] == "truncated"
    assert "next_char_offset" in first, "test setup: the line must be char-truncated"

    # Resume via char_offset — through the SAME router-facing handler this
    # issue reports as broken (not by hand-building a FileIROp).
    second = await _handle_read(
        {"path": "wide.txt", "offset": first["next_offset"], "char_offset": first["next_char_offset"]},
        tool_ctx,
    )
    assert second["status"] == "ok", (
        f"the resumed window ({len(huge_line) - first['next_char_offset']} chars) "
        f"must fit under the cap in one resume for this test's own sizing: {second}"
    )
    assert second["content"] == huge_line[first["next_char_offset"]:], (
        "char_offset was not actually forwarded from args into FileIROp — "
        "resuming at the reported position must recover the exact tail"
    )


def test_read_file_router_schema_declares_char_offset() -> None:
    """Tier 1: the router-facing JSON schema itself carries ``char_offset``
    — without this, an LLM cannot pass the value back even if the
    handler were fixed (schema + handler + description, all 3, per this
    issue's own scope)."""
    from reyn.tools.file import READ_FILE

    props = READ_FILE.parameters["properties"]
    assert "char_offset" in props
    assert props["char_offset"]["type"] == "integer"


def test_truncated_note_names_char_offset_when_present() -> None:
    """Tier 2: the human-readable ``note`` on a char-truncated result must
    say to pass BOTH ``offset`` AND ``char_offset`` back — the previous
    text named only ``offset``, which restarts the same oversized line
    from character 0 and truncates identically (an infinite loop the
    LLM had no textual cue to avoid)."""
    huge_line = "z" * (CAP + 5000)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        (tmp_path / "wide.txt").write_text(huge_line)
        result = _read(tmp_path, _ctx(tmp_path), path="wide.txt")
    assert "next_char_offset" in result
    assert "char_offset" in result["note"], (
        f"the resume hint must mention char_offset when a line was "
        f"char-truncated: {result['note']!r}"
    )


# ── ③ re-reading a tool-result SPILL whole errors with the remedy ──────────


@pytest.mark.asyncio
async def test_bare_reread_of_a_spilled_file_errors_with_the_remedy(tmp_path: Path) -> None:
    """Tier 2: a file that IS a real tool-result spill (written via
    ``MediaStore.save_tool_result``, the real production write path — not
    a hand-placed file that merely LOOKS like one) errors with the
    prescriptive remedy when a bare re-read of it would otherwise come
    back truncated, instead of silently truncating into the loop this
    issue traces (op_runtime's cap and the router's separate token cap
    are independent, so a truncated-but-still-big result could spill
    again)."""
    store = MediaStore(project_root=tmp_path, agent_name="test-agent", session_id="test-session")
    huge = "line content here, " * 5000  # large enough to force truncation on re-read
    block = store.save_tool_result(huge, tool="some_tool")
    spill_path = block["path"]  # project-relative, as a subsequent read_file would receive it
    # #5364 §1.4: the actual disk write is now off-loop (fire-and-forget) —
    # a real chat turn only ever re-reads a ref in a LATER LLM round (the
    # model must see the ref before it can name it, and every round
    # boundary passes through router_loop.py's own flush barrier first),
    # so this durability wait is what a real read always gets for free.
    await store.flush()

    result = await handle(
        FileIROp(kind="file", op="read", path=spill_path),
        _ctx(tmp_path, media_store=store),
    )

    assert result["status"] == "error"
    assert result["content"] == ""
    assert "char_offset" in result["error"], (
        f"the remedy must name char_offset, not just say 'too big': {result['error']!r}"
    )
    assert "spill" in result["error"].lower()


@pytest.mark.asyncio
async def test_spill_guard_survives_a_fresh_media_store_instance(tmp_path: Path) -> None:
    """Tier 2: lead-coder review (#4432) — a REFERENCE to a spill path can
    outlive the process that wrote it (it can sit in ``history.jsonl``,
    read back and re-issued as a plain ``read_file`` after a restart). An
    in-memory-ONLY registry would be empty in the next process and the
    guard would silently not fire — the loop this issue closes would
    reopen across the restart boundary. Simulated here with a SECOND,
    independently-constructed ``MediaStore`` pointed at the same
    ``project_root`` (the same object a fresh process would build) rather
    than reusing the first instance — this is what actually falsifies
    "in-memory only" if the persistence regresses."""
    writer = MediaStore(project_root=tmp_path, agent_name="test-agent", session_id="test-session")
    huge = "line content here, " * 5000
    block = writer.save_tool_result(huge, tool="some_tool")
    spill_path = block["path"]
    # #5364 §1.4: writer's own manifest-append is off-loop too (chained
    # after the content write, same worker, FIFO — see save_tool_result's
    # own comment) — a real restart only ever observes a manifest entry
    # AFTER real wall-clock time has passed (process exit/relaunch), which
    # this flush stands in for; a fresh instance racing the SAME process's
    # still-in-flight write is not a scenario a real restart can produce.
    await writer.flush()

    reader = MediaStore(project_root=tmp_path, agent_name="test-agent", session_id="test-session")  # a FRESH instance — simulates a new process
    assert reader.is_history_content_spill(spill_path), (
        "a fresh MediaStore instance (same project_root) must recognize a spill "
        "written by an earlier instance — the guard must survive a restart"
    )

    result = await handle(
        FileIROp(kind="file", op="read", path=spill_path),
        _ctx(tmp_path, media_store=reader),
    )
    assert result["status"] == "error", (
        "the read-time guard must fire through the FRESH instance too, not just "
        "the one that wrote the spill"
    )


@pytest.mark.asyncio
async def test_a_non_spill_oversized_file_still_truncates_normally(tmp_path: Path) -> None:
    """Tier 2: accept-side — an ordinary large file the store never wrote
    (i.e. NOT a spill) still gets the normal truncate-and-resume
    behaviour, unaffected by the new guard. Without this, the guard could
    have been implemented as "any oversized read errors", which would be
    a real regression for every non-spill large-file read in the repo."""
    store = MediaStore(project_root=tmp_path, agent_name="test-agent", session_id="test-session")
    huge_line = "w" * 50_000
    (tmp_path / "ordinary.txt").write_text(huge_line)

    result = await handle(
        FileIROp(kind="file", op="read", path="ordinary.txt"),
        _ctx(tmp_path, media_store=store),
    )

    assert result["status"] == "truncated"
    assert "error" not in result


def test_the_guard_is_a_no_op_with_no_media_store(tmp_path: Path) -> None:
    """Tier 1: accept-side — a caller with no ``media_store`` at all
    (``OpContext.media_store`` defaults to ``None`` — the pre-#4381
    shape every existing op_runtime test in this module already
    constructs) must behave exactly as before: normal truncation, never
    a crash from calling a method on ``None``."""
    huge_line = "v" * 50_000
    (tmp_path / "solo.txt").write_text(huge_line)

    result = _read(tmp_path, _ctx(tmp_path, media_store=None), path="solo.txt")

    assert result["status"] == "truncated"
