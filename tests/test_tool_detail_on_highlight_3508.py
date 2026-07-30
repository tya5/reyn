"""#3508 — a settled tool row shows its FULL result while the highlight is on it.

The one-line summary (`⎿ Read 42 lines`) is what the pane normally shows; the
raw result survives on the frame, so "expand" is a different rendering of data
the row already carries. Moving the highlight onto the row unfolds it and moving
away folds it back — no extra keystroke, which is the whole point: the owner's
report on Claude Code's expand was **"使いづらいから使ってない"** (not "I don't
want it"), so an affordance that costs a deliberate action was the thing that
failed, not the capability.

Asserted on the PAINTED body via the presenter, never on the meta flag: the flag
is an implementation detail of how the presenter is told, and a test that
watched it would pass even if nothing reached the screen.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat._meta_keys import (
    RESULT_KIND_KEY,
    RESULT_META_KEY,
)
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransport):
    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str) -> None:  # pragma: no cover
        pass

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass

    async def deliver_pending_answer(self, text: str) -> bool:
        return False


def _settled_tool(result: object, tool: str = "read_file") -> OutboxMessage:
    """A settled (coalesced) tool frame — the shape the live path and the restore
    projector both produce once a result folds into its started row."""
    return OutboxMessage(
        kind="tool_call_started",
        text=tool,
        meta={
            "tool": tool,
            "args": {"path": "a.py"},
            RESULT_KIND_KEY: "tool_call_completed",
            RESULT_META_KEY: {"result": result},
        },
    )


def _painted(flow: FlowView) -> str:
    return "\n".join(
        "".join(seg.text for seg in flow.render_line(y))
        for y in range(flow.size.height)
    )


async def _focus(pilot, app) -> FlowView:
    app.query_one(Composer).focus()
    await pilot.pause()
    await pilot.press("shift+tab")
    await pilot.pause()
    return app.query_one(FlowView)


@pytest.mark.asyncio
async def test_the_highlighted_tool_row_shows_the_full_result() -> None:
    """Tier 2b: the row summarised as one line unfolds to the whole result when
    the highlight arrives — and the summary line stays, so the row reads the
    same shape expanded or not."""
    # A LIST is the clearest hidden case: the summary collapses it to "3 items"
    # and shows none of the content. (A short scalar result is NOT hidden — the
    # summary falls back to the value itself, so such a row looks the same
    # expanded or not, which is correct: nothing was being withheld.)
    detail = ["line one", "line two", "line three"]
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(_settled_tool(detail))
        await pilot.pause()
        flow = app.query_one(FlowView)
        assert "3 items" in _painted(flow), (
            "test setup: the row is not showing the collapsed summary"
        )
        assert "line two" not in _painted(flow), (
            "test setup: the detail is already on screen before the highlight arrives"
        )

        await _focus(pilot, app)
        painted = _painted(flow)
        assert "line two" in painted and "line three" in painted, (
            f"the highlighted tool row did not unfold; pane shows:\n{painted}"
        )
        assert "⎿" in painted, "the summary line was replaced instead of extended"


@pytest.mark.asyncio
async def test_moving_the_highlight_away_folds_the_row_back() -> None:
    """Tier 2b: the expansion follows the highlight — the row left behind
    returns to its one-line summary, so a walk through the log does not leave a
    trail of unfolded rows that pushes everything off screen."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(_settled_tool(["expanded detail line", "second"]))
        app.conversation.append(OutboxMessage(kind="agent", text="a later reply"))
        await pilot.pause()
        flow = await _focus(pilot, app)

        await pilot.press("up")
        await pilot.pause()
        assert "expanded detail line" in _painted(flow), (
            "setup: the highlight did not reach the tool row"
        )

        await pilot.press("down")
        await pilot.pause()
        assert "expanded detail line" not in _painted(flow), (
            "the row stayed unfolded after the highlight left it"
        )


@pytest.mark.asyncio
async def test_an_ordinary_row_is_untouched_by_the_highlight() -> None:
    """Tier 2b: only rows with a folded result take part. An agent reply is not
    marked and not re-presented, so walking ordinary conversation costs nothing
    and its body cannot change under the highlight."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="an ordinary reply"))
        await pilot.pause()
        flow = app.query_one(FlowView)
        before = _painted(flow)

        await _focus(pilot, app)
        entry = flow.highlighted
        assert entry is not None, "setup: the highlight never arrived"
        assert (entry.item.meta or {}).get("_expanded") is None, (
            "an ordinary row was marked expanded — only rows with a folded "
            "result should take part"
        )
        after = _painted(flow)
        assert "an ordinary reply" in after, "the reply stopped rendering"
        assert before.count("an ordinary reply") == after.count("an ordinary reply"), (
            "the ordinary row was re-presented differently under the highlight"
        )


@pytest.mark.asyncio
async def test_a_huge_result_is_capped_and_says_so() -> None:
    """Tier 2b: a large result does not shove the conversation off screen.

    The expansion is triggered by the highlight merely ARRIVING, so an uncapped
    body would be a side effect of pressing ``k`` that the reader never asked
    for. The cap is stated in the row rather than silently truncating, and the
    untruncated text stays reachable through the row's own copy."""
    from reyn.interfaces.inline.textual_chat.presenter import _EXPANDED_MAX_LINES

    detail = [f"line {i}" for i in range(_EXPANDED_MAX_LINES + 25)]
    app = TextualChatApp(transport=_Transport())
    # Tall enough for the whole capped body to be ON SCREEN — at 30 rows the
    # trailer scrolls out of the viewport and the assertion would be about
    # clipping rather than about the cap.
    async with app.run_test(size=(100, 60)) as pilot:
        await pilot.pause()
        app.conversation.append(_settled_tool(detail))
        await pilot.pause()
        flow = await _focus(pilot, app)

        painted = _painted(flow)
        assert "more lines" in painted, (
            f"a capped result did not say it was capped; pane shows:\n{painted}"
        )
        assert f"line {_EXPANDED_MAX_LINES + 24}" not in painted, (
            "the cap did not hold — the whole result was rendered"
        )


@pytest.mark.asyncio
async def test_a_failed_tool_row_keeps_its_failure_line() -> None:
    """Tier 2b: a FAILURE row is not expanded. Its body is already the error
    message rather than a summary hiding a result, and it carries the failure
    tint — unfolding it would replace a deliberate, legible failure render with
    a raw dump (the #3367 don't-fight-the-failure-vocabulary boundary)."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        failed = OutboxMessage(
            kind="tool_call_started",
            text="read_file",
            meta={
                "tool": "read_file",
                RESULT_KIND_KEY: "tool_call_failed",
                RESULT_META_KEY: {"error_message": "no such file"},
            },
        )
        app.conversation.append(failed)
        await pilot.pause()
        flow = await _focus(pilot, app)

        painted = _painted(flow)
        assert "no such file" in painted, "the failure message stopped rendering"
        assert "✗" in painted, "the failure row lost its failure marker"


@pytest.mark.asyncio
async def test_a_result_the_summary_already_shows_is_not_duplicated() -> None:
    """Tier 2b: a row whose summary ALREADY shows the whole result does not
    unfold into the same sentence twice.

    ``summarize_tool_result`` falls back to the value itself for a short scalar,
    so "the detail" and "the summary" are the same string — expanding printed it
    on two consecutive lines. Found in a REAL terminal (the other tests here use
    list results, which the summary always collapses to "N items", so none of
    them could see it): the fix is to unfold only when the summary is actually
    withholding something."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(_settled_tool("Presented to the user. rows=0"))
        await pilot.pause()
        flow = await _focus(pilot, app)

        painted = _painted(flow)
        assert painted.count("Presented to the user. rows=0") == 1, (
            "the highlighted row repeated its own summary as 'detail':\n"
            f"{painted}"
        )
