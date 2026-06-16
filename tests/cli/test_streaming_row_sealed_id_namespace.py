"""Tier 2: sealed widget IDs are scoped per-row to avoid DOM collisions (F-F12).

Wave-9 Topic F finding F12 (P1): ``_apply_markdown_swap`` mounted
children with hardcoded ``id="sealed_prefix"`` and
``id="sealed_markdown"``. Textual widget IDs must be unique across
the whole DOM — when two StreamingRow instances sealed
concurrently (e.g. ``/attach`` mid-stream, remote ``--connect``
delivering an out-of-order ``__stream_end__`` for an earlier
turn), both attempts mounted children with the same IDs. Textual
logs duplicate-ID warnings and ``query_one`` lookups become
ambiguous; the ``except Exception: pass`` fallback in
``_apply_markdown_swap`` could silently swallow the error, falling
one of the sealed rows back to raw text.

After the fix the sealed children use ``id=f"{row_id}_prefix"``
and ``id=f"{row_id}_markdown"``. Since the StreamingRow's own id
is unique (production sets it to ``stream_<msg_id[:8]>``), the
sealed children inherit that uniqueness.

Public surfaces tested:
  - one StreamingRow → sealed children carry the row-scoped ids
  - two concurrently-sealed StreamingRows → all 4 child ids are
    distinct (= no DOM collision)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_single_row_sealed_ids_use_row_id_prefix() -> None:
    """Tier 2: sealed prefix + markdown ids include the row's own id."""
    from textual.widgets import Markdown, Static

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("abcd1234", "test-agent")
        row.append("hello")
        await pilot.pause()
        row.seal()
        await pilot.pause()

        # StreamingRow.id = "stream_abcd1234" (= first 8 chars of msg_id)
        # so sealed children IDs are "stream_abcd1234_prefix" /
        # "stream_abcd1234_markdown".
        row_id = row.id
        assert row_id is not None
        prefix_children = list(row.query(Static))
        # The visible Static may be hidden but still present; filter
        # to the sealed prefix specifically.
        sealed_prefixes = [s for s in prefix_children if s.id == f"{row_id}_prefix"]
        sealed_markdowns = [m for m in row.query(Markdown) if m.id == f"{row_id}_markdown"]
        assert sealed_prefixes, (
            f"expected sealed prefix with id={row_id}_prefix; "
            f"got static ids: {[s.id for s in prefix_children]}"
        )
        assert sealed_markdowns, (
            f"expected sealed markdown with id={row_id}_markdown; "
            f"got markdown ids: {[m.id for m in row.query(Markdown)]}"
        )


@pytest.mark.asyncio
async def test_two_concurrent_sealed_rows_have_distinct_child_ids() -> None:
    """Tier 2: two StreamingRow seals don't produce duplicate child IDs.

    The collision the fix prevents: pre-fix, both rows mounted
    ``id="sealed_prefix"`` + ``id="sealed_markdown"`` siblings. Post-fix
    each row's children inherit the row's own unique id.
    """
    from textual.widgets import Markdown, Static

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView
    from reyn.tui.widgets.streaming_row import StreamingRow

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        row_a = conv.begin_stream("aaaa1111", "agent-a")
        row_a.append("reply A")
        row_b = conv.begin_stream("bbbb2222", "agent-b")
        row_b.append("reply B")
        await pilot.pause()

        row_a.seal()
        row_b.seal()
        await pilot.pause()

        # Collect the SEALED child ids only (= what F-F12 addresses).
        # The per-row Static created during compose() carries
        # ``id="streaming_text"`` — that's a pre-existing concern out
        # of scope for this fix, so we filter to the sealed children
        # by their ``*_prefix`` / ``*_markdown`` id suffix.
        sealed_prefix_ids: list[str] = []
        sealed_md_ids: list[str] = []
        for row in conv.query(StreamingRow):
            for s in row.query(Static):
                if s.id and s.id.endswith("_prefix"):
                    sealed_prefix_ids.append(s.id)
            for m in row.query(Markdown):
                if m.id and m.id.endswith("_markdown"):
                    sealed_md_ids.append(m.id)

        # All sealed ids must be unique (= no two share the same id).
        combined = sealed_prefix_ids + sealed_md_ids
        assert len(combined) == len(set(combined)), (
            f"duplicate sealed child IDs detected after concurrent seal: "
            f"prefix={sealed_prefix_ids!r}, markdown={sealed_md_ids!r}"
        )

        # The legacy hardcoded ids must NOT appear anywhere.
        assert "sealed_prefix" not in combined
        assert "sealed_markdown" not in combined
        # Each row should have contributed one prefix + one markdown.
        row_a_prefix, row_b_prefix = sealed_prefix_ids
        row_a_md, row_b_md = sealed_md_ids
