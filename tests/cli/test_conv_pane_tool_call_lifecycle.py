"""Tier 2: ConversationView mounts + finalises ToolCallRow on outbox lifecycle.

issue #427 L4 step 4 — the conv pane's ``start_tool_call_row`` /
``complete_tool_call_row`` / ``fail_tool_call_row`` are the bridge
between forwarder-relayed outbox messages (= step 3) and the
ToolCallRow widget (= step 1 PoC).

Contract pinned here:

1. ``start_tool_call_row(op_id, tool, args_repr)`` mounts a ToolCallRow
   under the conv pane and keys it by ``op_id``.
2. Calling ``start_tool_call_row`` again with the same ``op_id`` is
   idempotent — returns the existing row instead of double-mounting.
3. ``complete_tool_call_row(op_id, result_snippet)`` transitions the
   row to its success terminal, flushes the rendered shape into the
   RichLog scrollback, and unmounts the live widget.
4. ``fail_tool_call_row(op_id, error)`` does the same with ✗ glyph.
5. Empty ``op_id`` short-circuits start_tool_call_row to None — we
   refuse to mount unkeyed rows that could never be finalised.
6. Calling complete / fail with an unknown ``op_id`` is a no-op
   (= forwarder bug or out-of-order delivery shouldn't blow up).

Plus the two app_outbox formatting helpers:
- ``_format_tool_args`` collapses bulky body fields to ``<N chars>``
- ``_format_tool_result`` handles dict / str / None gracefully
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.app_outbox import _format_tool_args, _format_tool_result  # noqa: E402
from reyn.chat.tui.widgets import ConversationView  # noqa: E402
from reyn.chat.tui.widgets.tool_call_row import ToolCallRow  # noqa: E402


class _ConvOnlyApp(App):
    def compose(self) -> ComposeResult:
        yield ConversationView(id="conversation")


# ── Conv pane lifecycle API tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_tool_call_row_mounts_widget_keyed_by_op_id():
    """Tier 2: mounting a tool-call row makes a ToolCallRow visible in the DOM."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_tool_call_row(
            "op-xyz", "read_file", args_repr="path=/tmp/x",
        )
        await pilot.pause()
        assert row is not None
        rows = list(conv.query(ToolCallRow))
        assert rows, "at least one ToolCallRow must be mounted"
        # The rendered line 1 carries the tool name + args.
        line1 = rows[0]._build_line1().plain
        assert "read_file" in line1
        assert "path=/tmp/x" in line1


@pytest.mark.asyncio
async def test_start_tool_call_row_is_idempotent_for_same_op_id():
    """Tier 2: same op_id doesn't double-mount; second call returns existing row."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        first = conv.start_tool_call_row("op-a", "read_file")
        second = conv.start_tool_call_row("op-a", "read_file")
        await pilot.pause()
        assert first is second
        assert len(list(conv.query(ToolCallRow))) == 1


@pytest.mark.flaky(reruns=2, reruns_delay=1)
@pytest.mark.asyncio
async def test_complete_tool_call_row_unmounts_live_widget():
    """Tier 2: success terminal removes the live row from the DOM.

    F-H min-display-time defers flush by up to 0.3s for very fast ops;
    the test waits past the threshold so the deferred unmount completes.
    The pause margin is sized generously (= 0.6s, 2× the F-H threshold)
    so CI runners under event-loop load can still meet the deadline
    without flaking — previously a 0.4s margin failed intermittently
    on Python 3.11 / 3.12 GitHub Actions runs during high concurrency.
    """
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.start_tool_call_row("op-b", "web_fetch")
        await pilot.pause()
        assert len(list(conv.query(ToolCallRow))) == 1
        conv.complete_tool_call_row("op-b", result_snippet="200 OK 1.2KB")
        # Wait well past the F-H min-display-time deferral so the
        # deferred unmount has a generous margin even under CI load.
        await pilot.pause(0.6)
        # Row is unmounted after flush.
        assert len(list(conv.query(ToolCallRow))) == 0


@pytest.mark.asyncio
async def test_fail_tool_call_row_unmounts_live_widget_and_records_error():
    """Tier 2: failure terminal removes the live row from the DOM."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.start_tool_call_row("op-c", "shell")
        await pilot.pause()
        conv.fail_tool_call_row("op-c", error="timeout")
        # Generous margin past the F-H 0.3s deferral so CI load doesn't
        # leave the row mounted past the assertion (= the 0.4s prior
        # margin was too tight under Python 3.11/3.12 GitHub Actions
        # concurrency).
        await pilot.pause(0.6)
        assert len(list(conv.query(ToolCallRow))) == 0


@pytest.mark.asyncio
async def test_fast_tool_call_defers_flush_so_row_stays_visible_briefly():
    """Tier 2b: very fast ops (= mount + complete in same tick) defer
    the flush so the user can perceive the row before it transitions
    to RichLog history. (F-H min-display-time mechanism)

    The "row is still visible briefly" check is performed
    SYNCHRONOUSLY (= no ``await``) immediately after
    ``complete_tool_call_row`` returns. The set_timer callback that
    unmounts the row only runs on a subsequent event-loop yield, so
    by not yielding we get a deterministic "deferred-flush is armed"
    signal without timing fragility. The prior
    ``await pilot.pause(0.05)`` was intended as "less than 0.3s"
    but on slow CI the cumulative wall time between mount and the
    pause assertion exceeded 0.3s — the timer fired during the
    pause and the assertion ``len == 1`` saw 0 instead.
    """
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.start_tool_call_row("op-fast", "cached_op", args_repr="key=x")
        await pilot.pause()
        # Complete immediately — F-H should defer the flush.
        conv.complete_tool_call_row("op-fast", result_snippet="ok")
        # Synchronous check: without yielding, the set_timer callback
        # cannot have fired, so the row must still be mounted. If the
        # deferred-flush was not armed (= the F-H mechanism broke),
        # ``complete_tool_call_row`` would have synchronously unmounted
        # the row via ``_do_flush_tool_call_row`` and this would fail.
        assert len(list(conv.query(ToolCallRow))) == 1, (
            "fast row stays mounted briefly so it's perceivable "
            "(= the F-H deferred-flush should be set; the row "
            "should NOT unmount synchronously on complete)"
        )
        # Wait past the threshold with generous CI margin — row
        # should be flushed by now.
        await pilot.pause(0.6)
        assert len(list(conv.query(ToolCallRow))) == 0


@pytest.mark.asyncio
async def test_empty_op_id_short_circuits_to_no_mount():
    """Tier 2: empty op_id refuses to mount — would be un-finalisable."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_tool_call_row("", "read_file")
        await pilot.pause()
        assert row is None
        assert len(list(conv.query(ToolCallRow))) == 0


@pytest.mark.asyncio
async def test_unknown_op_id_terminals_are_no_op():
    """Tier 2: terminal for unknown op_id doesn't crash or mount anything."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # No start_tool_call_row called for these op_ids.
        conv.complete_tool_call_row("never-mounted", result_snippet="x")
        conv.fail_tool_call_row("also-never-mounted", error="x")
        await pilot.pause()
        # No widgets mounted, no exception.
        assert len(list(conv.query(ToolCallRow))) == 0


@pytest.mark.asyncio
async def test_tool_call_with_parent_run_id_matching_skill_row_nests():
    """Tier 2b: tool_call whose run_id matches a mounted SkillActivityRow
    renders with a ``└─`` prefix so the nesting is visible. (F-F nesting)
    """
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # Mount a SkillActivityRow first (= "parent" of the eventual
        # tool_call) so the run_id lookup hits.
        conv.start_skill_row(run_id="parent-skill-run", skill_name="planner")
        await pilot.pause()
        row = conv.start_tool_call_row(
            "op-nested",
            "read_file",
            args_repr="path=/x",
            parent_run_id="parent-skill-run",
        )
        await pilot.pause()
        assert row is not None
        line1 = row._build_line1().plain
        assert "└─" in line1, "nested tool_call carries └─ prefix"
        assert "read_file" in line1


@pytest.mark.asyncio
async def test_tool_call_with_unmatched_parent_run_id_renders_root_level():
    """Tier 2b: tool_call whose run_id doesn't match any mounted skill
    row falls back to root-level rendering (= no └─ prefix). (F-F nesting)
    """
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # No skill rows mounted — parent_run_id has nothing to match.
        row = conv.start_tool_call_row(
            "op-root",
            "read_file",
            args_repr="path=/x",
            parent_run_id="non-existent-run",
        )
        await pilot.pause()
        assert row is not None
        line1 = row._build_line1().plain
        assert "└─" not in line1, "root-level tool_call has no prefix"


# ── app_outbox formatter helper tests ─────────────────────────────────────────


def test_format_tool_args_empty_and_non_dict():
    """Tier 2: empty / non-dict inputs produce empty string."""
    assert _format_tool_args(None) == ""
    assert _format_tool_args({}) == ""


def test_format_tool_args_keys_and_values():
    """Tier 2: dict args produce ``key=value, ...`` line."""
    out = _format_tool_args({"path": "/tmp/x", "limit": 50})
    assert "path=/tmp/x" in out
    assert "limit=50" in out


def test_format_tool_args_collapses_bulky_fields():
    """Tier 2: long ``content``-class fields become ``<N chars>`` placeholder."""
    out = _format_tool_args({"path": "/x", "content": "y" * 5000})
    assert "<5000 chars>" in out
    assert "yyyy" not in out


def test_format_tool_result_dict_string_none():
    """Tier 2: result formatter handles dict / str / None gracefully."""
    assert _format_tool_result(None) == ""
    assert _format_tool_result("simple text") == "simple text"
    out = _format_tool_result({"status": "ok", "exit_code": 0})
    assert "status=ok" in out
    assert "exit_code=0" in out


def test_format_tool_result_collapses_bulky_body():
    """Tier 2: result with bulky body field collapses to ``<N chars>``."""
    out = _format_tool_result({"status": "ok", "body": "z" * 5000})
    assert "<5000 chars>" in out


def test_format_tool_result_skips_redundant_kind_and_op_keys():
    """Tier 2: ``kind`` and ``op`` fields are already encoded in the tool
    name on line 1 — surfacing them again in the result snippet is noise.

    F-C (wave-#427 follow-up): smoke output showed
    ``kind=file, op=read, path=..., status=ok, ...`` consuming half the
    line width with information the user already has from line 1's
    ``file__read(...)``. Skipping them lets ``status`` / ``exit_code``
    / specific result fields land in the visible budget instead.
    """
    out = _format_tool_result({
        "kind": "file",
        "op": "read",
        "status": "ok",
        "exit_code": 0,
        "path": "/tmp/x.txt",
    })
    # Redundant keys are gone.
    assert "kind=" not in out
    assert "op=" not in out
    # Informative fields remain.
    assert "status=ok" in out
    assert "exit_code=0" in out
    assert "path=/tmp/x.txt" in out


def test_format_tool_result_truncates_long_string_input():
    """Tier 2: very long plain-string result gets ellipsised."""
    out = _format_tool_result("x" * 500)
    assert out.endswith("...")


def test_format_tool_result_drops_trailing_fields_to_keep_placeholders_atomic():
    """Tier 2: ``<N chars>`` placeholder must survive whole — drop trailing
    fields rather than truncating into the placeholder string.

    Wave-#427 smoke detected ``content=<3 cha…`` (= placeholder broken
    mid-string) when the joined result exceeded the body budget. The
    fixed formatter drops trailing fields atomically; the placeholder
    is either present in full or dropped entirely — never truncated
    into a meaningless fragment.
    """
    # Many small fields plus a placeholder-eligible bulky one at the end
    # — the assembled string blows past the budget, so trailing fields
    # should be dropped while earlier ones survive intact.
    # Note: ``kind`` + ``op`` are skipped by ``_format_tool_result`` per
    # F-C (PR #448), so the surviving earlier field is ``path`` (= first
    # non-redundant key in dict order).
    result = {
        "kind": "file",
        "op": "read",
        "path": "some/really/long/path/that/eats/up/many/cells.toml",
        "status": "ok",
        "exit_code": 0,
        "extra_field_1": "value_one",
        "extra_field_2": "value_two",
        "content": "x" * 5000,
    }
    out = _format_tool_result(result)
    # No partial placeholder fragments — if `content=` appears, the full
    # `<5000 chars>` must follow; otherwise it shouldn't appear at all.
    if "content=" in out:
        assert "<5000 chars>" in out, (
            f"placeholder broken mid-string: {out!r}"
        )
    # Earlier, more-important fields preserved (= ``path`` is the first
    # non-redundant field after F-C's kind/op skip).
    assert "path=" in out


def test_format_tool_result_short_result_unchanged():
    """Tier 2: a result that fits the budget passes through verbatim."""
    out = _format_tool_result({"status": "ok", "exit_code": 0, "bytes": 1234})
    assert out == "status=ok, exit_code=0, bytes=1234"


# ── abort_tool_call_rows sweep (C-F1 wave-8) ──────────────────────────────────


@pytest.mark.flaky(reruns=2, reruns_delay=1)
@pytest.mark.asyncio
async def test_abort_tool_call_rows_seals_live_rows_with_aborted_terminal():
    """Tier 2b: ``abort_tool_call_rows`` finishes every live row as ⊘. (C-F1 sweep)

    The intended call site is ``ReynTUIApp.action_cancel_inflight``;
    without this sweep, in-flight tool_call widgets stayed mounted as
    ``●`` spinners and eventually got flushed to RichLog still in the
    running state (= frozen spinner in scroll history).
    """
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.start_tool_call_row("op-1", "read_file", args_repr="path=/a")
        conv.start_tool_call_row("op-2", "web_fetch", args_repr="url=...")
        await pilot.pause()
        assert len(list(conv.query(ToolCallRow))) == 2
        cancelled = conv.abort_tool_call_rows(reason="cancelled")
        assert cancelled == 2
        # Allow F-H min-display-time deferral to elapse before checking
        # the unmount — same wait pattern the other lifecycle tests use.
        await pilot.pause(0.4)
        assert len(list(conv.query(ToolCallRow))) == 0


@pytest.mark.asyncio
async def test_abort_tool_call_rows_returns_zero_when_no_live_rows():
    """Tier 2: idempotent — no live rows → returns 0, no exception."""
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # No start_tool_call_row called — registry empty.
        assert conv.abort_tool_call_rows(reason="cancelled") == 0


@pytest.mark.asyncio
async def test_abort_tool_call_rows_after_complete_is_noop():
    """Tier 2: rows already in a terminal state are not double-counted.

    ``complete_tool_call_row`` pops the row out of ``_tool_call_rows``
    before the deferred flush, so ``abort_tool_call_rows`` should
    find an empty registry and return 0.
    """
    app = _ConvOnlyApp()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.start_tool_call_row("op-c", "read_file")
        await pilot.pause()
        conv.complete_tool_call_row("op-c", result_snippet="ok")
        # Even before the F-H deferred flush elapses, the row is already
        # popped from the registry so abort sees nothing.
        assert conv.abort_tool_call_rows(reason="cancelled") == 0
