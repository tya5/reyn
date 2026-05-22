"""Tier 2: InputBar swallows duplicate Enter while turn is in flight (D-F11).

Wave-9 Topic D finding F11 (P1): the TextArea stayed enabled while a
prior turn was still in flight (= LLM streaming), so a fast Enter /
Enter sequence dispatched the same prompt twice. Doubled spend and
duplicated reply in the conv pane.

Now ``InputBar`` carries an ``_in_flight`` flag. ``_submit`` sets it
immediately after posting ``UserSubmitted`` and returns early on
subsequent calls. The App releases the lock at lifecycle boundaries
(stream end, skill done with empty queue, Ctrl+C cancel, slash-command
return) via the public ``set_in_flight`` method.

Public surfaces tested:
  - ``set_in_flight`` toggles the ``in-flight`` CSS class idempotently
  - second ``_submit`` during in-flight does NOT post a second
    ``UserSubmitted`` and does NOT clear the TextArea (= typed text
    preserved for re-submit after unlock)
  - ``action_cancel_inflight`` (Ctrl+C) releases the lock
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_set_in_flight_toggles_css_class_idempotently() -> None:
    """Tier 2: ``set_in_flight`` is idempotent + toggles ``in-flight`` class."""
    from reyn.chat.tui.widgets import InputBar

    bar = InputBar()
    # Default state.
    assert bar._in_flight is False
    assert not bar.has_class("in-flight")

    bar.set_in_flight(True)
    assert bar._in_flight is True
    assert bar.has_class("in-flight")

    # Idempotent re-set is a no-op.
    bar.set_in_flight(True)
    assert bar._in_flight is True
    assert bar.has_class("in-flight")

    bar.set_in_flight(False)
    assert bar._in_flight is False
    assert not bar.has_class("in-flight")

    # Idempotent un-set.
    bar.set_in_flight(False)
    assert bar._in_flight is False
    assert not bar.has_class("in-flight")


@pytest.mark.asyncio
async def test_submit_during_in_flight_swallows_text_unchanged() -> None:
    """Tier 2: ``_submit`` during in-flight preserves text + posts no message.

    Uses the full Textual harness to drive a real TextArea + post_message
    queue. Spy on ``UserSubmitted`` via a list mutated from a wrapper
    around ``post_message``.
    """
    from textual.widgets import TextArea

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    posted: list[InputBar.UserSubmitted] = []

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        ta = app.query_one("#input", TextArea)

        # Spy on InputBar.post_message — capture every ``UserSubmitted``
        # the widget produces and intercept it (do NOT forward to the
        # real queue). We're testing the InputBar's emission contract,
        # not the downstream App routing — forwarding the messages
        # would trigger ``on_input_bar_user_submitted`` which expects
        # a fully-wired session and a #conversation widget. Other
        # message types (e.g. UserSubmitted siblings) bypass the
        # capture and forward normally.
        original_post = bar.post_message

        def _spy(msg):  # type: ignore[no-untyped-def]
            if isinstance(msg, InputBar.UserSubmitted):
                posted.append(msg)
                return True  # match Textual's post_message return shape
            return original_post(msg)

        bar.post_message = _spy  # type: ignore[method-assign]

        # First submit: text populated, _submit called → posts + locks.
        ta.load_text("hi")
        bar._submit(ta)
        assert len(posted) == 1
        assert posted[0].text == "hi"
        assert bar._in_flight is True
        assert bar.has_class("in-flight")
        assert ta.text == "", "first submit should clear the TextArea"

        # Second submit while locked: text NOT cleared, NO new message.
        ta.load_text("yo")
        bar._submit(ta)
        assert len(posted) == 1, (
            f"second submit slipped through while in-flight: {posted!r}"
        )
        assert ta.text == "yo", (
            f"text cleared on swallowed submit (lost user input): {ta.text!r}"
        )

        # Release the lock + submit again: posts normally.
        bar.set_in_flight(False)
        bar._submit(ta)
        assert len(posted) == 2
        assert posted[1].text == "yo"
        assert ta.text == ""


@pytest.mark.asyncio
async def test_ctrl_c_releases_in_flight_lock() -> None:
    """Tier 2: ``action_cancel_inflight`` unconditionally clears the lock.

    Even with no skills actually running, Ctrl+C is the documented
    escape hatch from a stuck state. The lock must release so the
    user can submit again.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import InputBar

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)

        bar.set_in_flight(True)
        assert bar.has_class("in-flight")

        app.action_cancel_inflight()
        await pilot.pause()
        assert bar._in_flight is False
        assert not bar.has_class("in-flight")
