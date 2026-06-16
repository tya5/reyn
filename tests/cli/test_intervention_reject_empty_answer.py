"""Tier 2: InterventionWidget rejects empty / whitespace-only answer (E-F2).

Wave-9 Topic E finding F2 (P1): pressing Enter on a blank Input
fired ``_submit("")``, which posted ``Answered("")`` and removed the
widget. The agent either silently mismatched ``match_choice``
(= stuck waiting for a real answer) or routed an empty string into
the conversation as a wrong decision.

``on_input_submitted`` now guards on ``event.value.strip()`` —
empty / whitespace-only values are silently ignored, leaving the
Input focused with its placeholder for the user to type a real
answer.

Public surfaces tested:
  - empty value Enter → no ``Answered`` message, widget stays mounted
  - whitespace-only Enter → no ``Answered`` message, widget stays mounted
  - non-empty Enter → ``Answered`` fires with the value, widget removes
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


async def _mount_widget(pilot, choices=None):
    """Mount a free-text-only InterventionWidget under the conv pane."""
    from reyn.tui.widgets import ConversationView
    from reyn.tui.widgets.intervention import InterventionWidget

    app = pilot.app
    conv = app.query_one("#conversation", ConversationView)
    widget = InterventionWidget(
        iv_id="empty-test-iv",
        question="answer me?",
        choices=choices or [],  # no chips → pure free-text mode
    )
    await conv.mount(widget)
    await pilot.pause()
    return widget


def _make_submitted_event(value: str):
    """Build a minimal Input.Submitted event carrying ``value``.

    Textual's Input.Submitted ``__init__`` requires the source Input
    instance; we construct a bare object with ``value`` + a no-op
    ``stop`` to drive ``on_input_submitted`` directly.
    """
    class _Stub:
        def __init__(self, v: str) -> None:
            self.value = v
        def stop(self) -> None:
            pass
    return _Stub(value)


@pytest.mark.asyncio
async def test_empty_value_enter_is_ignored() -> None:
    """Tier 2: Enter on empty Input does NOT post Answered or remove widget."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets.intervention import InterventionWidget

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    answered: list[str] = []

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        widget = await _mount_widget(pilot)

        # Spy on Answered emission via post_message wrapper.
        original_post = widget.post_message

        def _spy(msg):  # type: ignore[no-untyped-def]
            if isinstance(msg, InterventionWidget.Answered):
                answered.append(msg.answer)
                return True
            return original_post(msg)

        widget.post_message = _spy  # type: ignore[method-assign]

        await widget.on_input_submitted(_make_submitted_event(""))
        await pilot.pause()
        assert answered == [], f"empty Enter slipped through: {answered!r}"
        # Widget must remain mounted so the user can still type.
        assert app.query(InterventionWidget), "widget was removed on empty Enter"


@pytest.mark.asyncio
async def test_whitespace_only_value_enter_is_ignored() -> None:
    """Tier 2: Enter on whitespace-only Input is also ignored.

    Spaces / tabs / newlines alone aren't a real answer; same
    treatment as empty.
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets.intervention import InterventionWidget

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    answered: list[str] = []

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        widget = await _mount_widget(pilot)

        original_post = widget.post_message

        def _spy(msg):  # type: ignore[no-untyped-def]
            if isinstance(msg, InterventionWidget.Answered):
                answered.append(msg.answer)
                return True
            return original_post(msg)

        widget.post_message = _spy  # type: ignore[method-assign]

        await widget.on_input_submitted(_make_submitted_event("   \t  "))
        await pilot.pause()
        assert answered == [], f"whitespace Enter slipped through: {answered!r}"
        assert app.query(InterventionWidget)


@pytest.mark.asyncio
async def test_non_empty_value_enter_posts_answered_and_removes_widget() -> None:
    """Tier 2: real answer fires Answered + removes widget (regression)."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets.intervention import InterventionWidget

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    answered: list[str] = []

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        widget = await _mount_widget(pilot)

        original_post = widget.post_message

        def _spy(msg):  # type: ignore[no-untyped-def]
            if isinstance(msg, InterventionWidget.Answered):
                answered.append(msg.answer)
                return True
            return original_post(msg)

        widget.post_message = _spy  # type: ignore[method-assign]

        await widget.on_input_submitted(_make_submitted_event("yes please"))
        await pilot.pause()
        assert answered == ["yes please"]
        # Widget should have removed itself after a real submit.
        assert not app.query(InterventionWidget), (
            "widget should be removed after non-empty submit"
        )
