"""#3508 — a settled tool row shows its FULL result via its tool-detail fold.

The one-line summary (`⎿ Read 42 lines`) is what the pane normally shows; the
raw result survives on the frame, so "expand" is a different rendering of data
the row already carries.

#4697 (owner ruling, #4691 §6): the fold no longer follows the highlight —
moving the highlight and choosing to open/close ONE row's detail are two
different intents, and coupling them meant a reader could never move past a
big result without collapsing it first. Space (outside flowview's character-
cursor mode — `c` toggles that) is the dedicated open/close trigger now; the
highlight moves the read position without touching any row's fold state.

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
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransportStub):
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


async def _toggle_fold(pilot, flow: FlowView) -> None:
    """#4697: press Space to fold/unfold the highlighted entry — the
    dedicated trigger that replaced #3508's auto-expand-on-highlight.

    Waits on TWO actual conditions in sequence, rather than a fixed pause
    count (testing.md § Time): first the current entry's ``_EXPANDED_KEY``
    meta flag settling to its post-press value (Space now round-trips
    through ``ToggleFoldRequested``, a POSTED message handled on a LATER
    event-loop tick than #3508's original synchronous ``_set_expanded``
    call was), then the row's own rendered content clearing flowview's
    lazy-render ``"Loading..."`` placeholder (``Entry.update()``'s reflow
    is itself a further tick after the flag flips)."""
    from reyn.interfaces.inline.textual_chat._meta_keys import EXPANDED_KEY

    entry = flow.current
    assert entry is not None
    before = bool((entry.item.meta or {}).get(EXPANDED_KEY))
    await pilot.press("space")
    while bool((entry.item.meta or {}).get(EXPANDED_KEY)) == before:
        await pilot.pause()
    while "Loading..." in _painted(flow):
        await pilot.pause()


@pytest.mark.asyncio
async def test_space_unfolds_the_highlighted_tool_row() -> None:
    """Tier 2b: #4697 — the row summarised as one line unfolds to the whole
    result on Space, and the summary line stays, so the row reads the same
    shape expanded or not. Arriving via highlight ALONE (no Space) must NOT
    unfold it — #4691 §6's owner ruling decoupled the two."""
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
        assert "line two" not in _painted(flow), (
            "highlight arrival alone unfolded the row — #4697 decoupled this"
        )

        await _toggle_fold(pilot, flow)
        painted = _painted(flow)
        assert "line two" in painted and "line three" in painted, (
            f"Space did not unfold the highlighted tool row; pane shows:\n{painted}"
        )
        assert "⎿" in painted, "the summary line was replaced instead of extended"


@pytest.mark.asyncio
async def test_moving_the_highlight_away_no_longer_folds_the_row_back() -> None:
    """Tier 2b: #4697 — an EXPANDED row stays expanded when the highlight
    moves away (inverts #3508's original auto-fold-on-move-away — the
    whole point of decoupling movement from open/close: a reader can walk
    past a big result without it collapsing under them)."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(_settled_tool(["expanded detail line", "second"]))
        app.conversation.append(OutboxMessage(kind="agent", text="a later reply"))
        await pilot.pause()
        flow = await _focus(pilot, app)

        await pilot.press("up")
        await pilot.pause()
        await _toggle_fold(pilot, flow)
        assert "expanded detail line" in _painted(flow), (
            "setup: Space did not unfold the tool row"
        )

        await pilot.press("down")
        await pilot.pause()
        assert "expanded detail line" in _painted(flow), (
            "the row folded back after the highlight left it — #4697 decoupled this"
        )


@pytest.mark.asyncio
async def test_space_folds_an_expanded_row_back() -> None:
    """Tier 2b: #4697 — Space is a TOGGLE, not just an unfold — pressing it
    again on an already-expanded row folds it back."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(_settled_tool(["expanded detail line", "second"]))
        await pilot.pause()
        flow = await _focus(pilot, app)

        await _toggle_fold(pilot, flow)
        assert "expanded detail line" in _painted(flow), "setup: Space did not unfold"

        await _toggle_fold(pilot, flow)
        assert "expanded detail line" not in _painted(flow), (
            "a second Space press did not fold the row back"
        )


@pytest.mark.asyncio
async def test_space_in_character_cursor_mode_still_copies_not_folds() -> None:
    """Tier 2b: #4697's own explicit measurement/guard — while flowview's
    character-cursor mode is engaged (toggled by ``c``), Space must fall
    through to the ordinary copy commit, never fold, so an in-progress
    text selection is never disrupted."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(_settled_tool(["expanded detail line", "second"]))
        await pilot.pause()
        flow = await _focus(pilot, app)

        await pilot.press("c")  # engage character-cursor mode
        await pilot.pause()
        assert flow.cursor_visible, "setup: character-cursor mode did not engage"

        await pilot.press("space")
        await pilot.pause()
        assert "expanded detail line" not in _painted(flow), (
            "Space folded the row while the character cursor was engaged — "
            "the #4697 guard did not fire"
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
        entry = flow.current
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

    #4697: the expansion is now triggered by an explicit Space, not the
    highlight merely arriving — so an uncapped body is no longer a side
    effect of pressing ``k``/``j`` at all; the cap protects the deliberate
    Space press instead. The cap is stated in the row rather than silently
    truncating, and the untruncated text stays reachable through the row's
    own copy."""
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
        await _toggle_fold(pilot, flow)

        painted = _painted(flow)
        assert "more lines" in painted, (
            f"a capped result did not say it was capped; pane shows:\n{painted}"
        )
        assert f"line {_EXPANDED_MAX_LINES + 24}" not in painted, (
            "the cap did not hold — the whole result was rendered"
        )


@pytest.mark.asyncio
async def test_multiline_field_in_a_dict_result_expands_to_real_lines() -> None:
    """Tier 2b: #4756 — a dict result whose FIELD is itself a multi-line
    string (``exec``'s ``stdout``, ``read_file``'s ``content``) unfolds to
    its REAL lines, not one JSON-escaped line of literal ``\\n`` text. The
    pre-#4756 bug: ``json.dumps(result, indent=2)`` only inserts real
    newlines between dict structural elements, never inside a string
    VALUE's own escaped content — the whole point of expanding a row (see
    the content a reader opened it for) was defeated for exactly this
    shape."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(_settled_tool(
            {"kind": "sandboxed_exec", "status": "ok", "returncode": 0,
             "stdout": "line one\nline two\nline three", "stderr": "",
             "truncated": False, "denial_class": None},
            tool="exec",
        ))
        await pilot.pause()
        flow = await _focus(pilot, app)
        await _toggle_fold(pilot, flow)

        painted = _painted(flow)
        assert "line one" in painted and "line two" in painted and "line three" in painted, (
            f"the multi-line stdout field did not unfold to real lines; pane shows:\n{painted}"
        )
        assert "\\n" not in painted, (
            "the multi-line field rendered as one JSON-escaped line "
            f"(literal backslash-n) instead of real lines; pane shows:\n{painted}"
        )


@pytest.mark.asyncio
async def test_a_big_multiline_field_makes_the_cap_engage() -> None:
    """Tier 2b: #4756's own required permanent condition — the fix must
    make ``_EXPANDED_MAX_LINES`` actually ENGAGE for a dict result whose
    size lives inside one multi-line field, not just for a top-level list
    (the pre-existing ``test_a_huge_result_is_capped_and_says_so`` case).
    Before #4756, a big ``content``/``stdout`` field collapsed to ONE
    structural JSON line, so the line-count cap never saw the real size —
    the cap that exists specifically to stop a huge result shoving the
    conversation off-screen did not engage for this shape at all."""
    from reyn.interfaces.inline.textual_chat.presenter import _EXPANDED_MAX_LINES

    big_content = "\n".join(f"line {i}" for i in range(_EXPANDED_MAX_LINES + 25))
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 60)) as pilot:
        await pilot.pause()
        app.conversation.append(_settled_tool(
            {"op": "read", "path": "big.py", "status": "ok", "content": big_content},
        ))
        await pilot.pause()
        flow = await _focus(pilot, app)
        await _toggle_fold(pilot, flow)

        painted = _painted(flow)
        assert "more lines" in painted, (
            f"the cap did not engage for a big multi-line dict field; pane shows:\n{painted}"
        )
        assert f"line {_EXPANDED_MAX_LINES + 24}" not in painted, (
            "the cap did not hold — the whole multi-line field was rendered"
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
        await _toggle_fold(pilot, flow)

        painted = _painted(flow)
        assert painted.count("Presented to the user. rows=0") == 1, (
            "the expanded row repeated its own summary as 'detail':\n"
            f"{painted}"
        )
