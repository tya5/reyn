"""#3692 PR-B — the 3 boundary points reyn owns at the edge of the conversation
pane (Ctrl+O in, motions reaching flowview, Esc's own layer already pinned
elsewhere): flowview owns the interior (movement, selection, yank — #3692's
own non-goal is adding zero motions of reyn's own), so what these tests pin
is reyn's side of the seam, not flowview's contract.

Esc-layering has its own dedicated file, ``test_textual_chat_esc_sufficiency_
3365.py`` (states 2, 6, and the new 8th case added by this same arc) — not
duplicated here.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import Composer, TextualChatApp
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


async def _seeded(pilot, app, texts=("older reply", "newest reply")):
    for text in texts:
        app.conversation.append(OutboxMessage(kind="agent", text=text))
    await pilot.pause()
    app.query_one(Composer).focus()
    await pilot.pause()
    return app.query_one(FlowView)


@pytest.mark.asyncio
async def test_ctrl_o_focuses_the_conversation_pane_from_the_composer() -> None:
    """Tier 2b: #3692 PR-B ① — the one binding that is genuinely reyn's to add.
    flowview cannot bind a key for "I don't have focus yet"; only the app can
    move focus INTO it. ``Shift+Tab`` (Textual's own cycle-focus) already
    reaches the pane — this is a direct jump alongside it, not a
    replacement, so both stay live."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 20)) as pilot:
        flow = await _seeded(pilot, app)
        # Establish a REMEMBERED current entry first (a fresh, never-focused
        # pane's ``current`` is None — a first-focus initialisation is not
        # the "pure focus move" this test is pinning). Move off the initial
        # entry so a Ctrl+O that silently reset to the top would be caught.
        await pilot.press("shift+tab")
        await pilot.pause()
        await pilot.press("k")
        await pilot.pause()
        remembered = flow.current
        assert remembered is not None, "test setup: no current entry after Shift+Tab"

        app.query_one(Composer).focus()
        await pilot.pause()
        assert app.query_one(Composer).has_focus, "test setup: Composer did not retake focus"

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert app.focused is flow, (
            f"Ctrl+O did not focus the conversation pane: {app.focused!r}"
        )
        assert flow.current is remembered, (
            "Ctrl+O moved (or reset) the current entry — it must be a pure focus move"
        )


@pytest.mark.asyncio
async def test_ctrl_o_is_not_reyns_only_route_shift_tab_still_reaches_the_pane() -> None:
    """Tier 2b: Ctrl+O is additive — the existing Shift+Tab cycle-focus route
    into the pane must keep working unchanged (#3692's acceptance criteria:
    normal Composer input is otherwise untouched)."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 20)) as pilot:
        flow = await _seeded(pilot, app)

        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.focused is flow, (
            f"Shift+Tab no longer reaches the conversation pane: {app.focused!r}"
        )


def _cursor_col(flow: FlowView, row: int) -> "int | None":
    """The column of flowview's own text-cursor glyph on content row ``row``
    (``None`` if the cursor is not on this row) — read from
    :meth:`FlowView.render_line`'s PUBLIC per-segment ``style``, not any
    private cursor-position field.

    Compares against the app's OWN LIVE ``screen--selection`` component
    style, resolved dynamically, not a literal colour string. flowview's own
    docstring (``_view.py``, ``COMPONENT_CLASSES``) states the cursor "defers
    to Textual's ``screen--selection``" — that component's resolved
    background is what the cursor glyph actually paints, and #4840's owner
    ruling (mapping ``@selection-bg@`` to reyn's own theme, #4881) changed
    that resolved value from an ANSI-numbered colour (which Rich's ``Style``
    repr rendered as the literal substring ``"on color(4)"``, this helper's
    PRIOR check) to a concrete truecolor hex. A hardcoded string match
    encoded an incidental detail of ONE specific token value, not the actual
    invariant (the cursor renders with `.screen--selection`'s own
    background) — comparing against the live resolved style survives any
    future value that component resolves to, ANSI-numbered or concrete."""
    selection_bg = flow.screen.get_component_styles("screen--selection").background
    x = 0
    for seg in flow.render_line(row):
        if seg.style is not None and seg.style.bgcolor == selection_bg.rich_color:
            return x
        x += len(seg.text)
    return None


@pytest.mark.asyncio
async def test_motions_reach_flowview_not_witnessing_flowviews_own_contract() -> None:
    """Tier 2b: #3692's acceptance criteria — ``j/k``, ``ctrl+d/u``, ``g/G``,
    ``v/V/y`` must WORK, but the test must witness them REACHING flowview's
    own action (nothing in reyn intercepts or reimplements them), not assert
    what flowview does with them once they land — that is flowview's own
    test suite's contract, and pinning it here would have to be rewritten
    every time upstream tunes a motion (exactly what #3692 rules out).

    Two representative, cheap-to-observe motions stand in for the whole set
    (this file does not re-enumerate flowview's full keymap — that is
    ``test_no_reyn_surface_declares_a_flowview_owned_key`` in
    ``test_copy_mode_3507.py``, which pins the BOUNDARY, not the motions):
    ``l`` (character cursor moves right) and ``ctrl+d`` (viewport scrolls) —
    one from the text-cursor family, one from the scroll family, both
    observable through FlowView's PUBLIC ``render_line``/``scroll_offset``
    surface."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 10)) as pilot:
        for i in range(40):
            app.conversation.append(OutboxMessage(kind="agent", text=f"reply {i}"))
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("shift+tab")
        await pilot.pause()
        flow = app.focused
        assert isinstance(flow, FlowView)

        await pilot.press("c")  # show the text cursor
        await pilot.press("g")  # jump to the top entry, a known starting row
        flow.scroll_to_top(animate=False)
        await pilot.pause()

        cursor_row = next(
            (y for y in range(flow.size.height) if _cursor_col(flow, y) is not None),
            None,
        )
        assert cursor_row is not None, "test setup: the text cursor is not visible"
        before_col = _cursor_col(flow, cursor_row)

        await pilot.press("l")
        await pilot.pause()
        after_col = _cursor_col(flow, cursor_row)
        # #4304 (part of #3880): weakened from `== before_col + 1` — that pinned
        # flowview's own cursor-right STEP MAGNITUDE (its business, would break
        # on any upstream retune), not reyn's actual claim, which per this
        # test's own docstring is only that the key REACHED flowview's action.
        assert after_col != before_col, (
            f"'l' did not reach flowview's cursor-right action: "
            f"{before_col!r} -> {after_col!r}"
        )

        before_scroll = flow.scroll_offset.y
        await pilot.press("ctrl+d")
        await pilot.pause()
        after_scroll = flow.scroll_offset.y
        assert after_scroll > before_scroll, (
            f"ctrl+d did not reach flowview's scroll action: "
            f"{before_scroll!r} -> {after_scroll!r}"
        )
