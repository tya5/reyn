"""Tier 2: RenderableCacheMixin — shared cache pattern for Static-backed widgets.

After three widgets (SkillActivityRow, SlashPicker, soon ReynHeader)
duplicated the same ~10-line cache pattern for "what did I last
render?", the trigger to extract a shared mixin was hit. This pins:

  - ``_set_rendered_cache`` accepts both ``Text`` and ``str``
  - ``rendered_text()`` returns the plain text in both cases
  - Empty / pre-set / pre-mount returns ``""``
  - Subclasses keep their ``rendered_text()`` accessor via the mixin
    (= existing SkillActivityRow / SlashPicker tests still pass)
  - The mixin doesn't pollute MRO ordering of the host widget
    (= ``isinstance`` checks against the framework base still work)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_mixin_accepts_text_renderable() -> None:
    """Tier 2: Text input round-trips through ``rendered_text``."""
    from rich.text import Text

    from reyn.chat.tui.widgets._renderable_cache import RenderableCacheMixin

    obj = RenderableCacheMixin()
    obj._set_rendered_cache(Text("hello world"))
    assert obj.rendered_text() == "hello world"


def test_mixin_accepts_str_renderable() -> None:
    """Tier 2: str input is wrapped + plain text returned."""
    from reyn.chat.tui.widgets._renderable_cache import RenderableCacheMixin

    obj = RenderableCacheMixin()
    obj._set_rendered_cache("plain string")
    assert obj.rendered_text() == "plain string"


def test_mixin_default_returns_empty() -> None:
    """Tier 2: pre-set / fresh instance returns ``""``."""
    from reyn.chat.tui.widgets._renderable_cache import RenderableCacheMixin

    obj = RenderableCacheMixin()
    assert obj.rendered_text() == ""


def test_mixin_empty_string_round_trip() -> None:
    """Tier 2: explicit empty string sets a non-None cache that reads as ``""``."""
    from rich.text import Text

    from reyn.chat.tui.widgets._renderable_cache import RenderableCacheMixin

    obj = RenderableCacheMixin()
    obj._set_rendered_cache("")
    assert obj.rendered_text() == ""
    # Now overwrite with non-empty and verify replacement.
    obj._set_rendered_cache(Text("non-empty"))
    assert obj.rendered_text() == "non-empty"


def test_mixin_styled_text_yields_plain_text() -> None:
    """Tier 2: styling on the source Text doesn't leak into the plain output.

    Pins that ``rendered_text`` is the plain rendering — markup /
    styling on the source Text is stripped. Otherwise tests would
    have to grep around ANSI / markup sequences.
    """
    from rich.text import Text

    from reyn.chat.tui.widgets._renderable_cache import RenderableCacheMixin

    obj = RenderableCacheMixin()
    styled = Text()
    styled.append("hello", style="bold red")
    styled.append(" ", style="dim")
    styled.append("world", style="italic blue")
    obj._set_rendered_cache(styled)
    out = obj.rendered_text()
    assert "hello" in out
    assert "world" in out
    # No ANSI escape sequences leak through.
    assert "\x1b[" not in out


@pytest.mark.asyncio
async def test_skill_activity_row_uses_mixin_accessor() -> None:
    """Tier 2: SkillActivityRow.rendered_text comes from the mixin.

    Regression guard — refactor must not silently remove the
    accessor or change its signature.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets._renderable_cache import RenderableCacheMixin

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="mixinaa", skill_name="mxn")
        row.set_phase("plan")
        await pilot.pause()
        # Accessor exists + returns string.
        text = row.rendered_text()
        assert isinstance(text, str)
        assert "plan" in text
        # Inheritance includes the mixin (= refactor landed).
        assert isinstance(row, RenderableCacheMixin)


@pytest.mark.asyncio
async def test_slash_picker_uses_mixin_accessor() -> None:
    """Tier 2: SlashPicker.rendered_text comes from the mixin.

    Pins same contract as the SkillActivityRow case — picker
    keeps its public rendered_text() accessor via the mixin.
    """
    from reyn.chat.slash import SlashCommand
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets._renderable_cache import RenderableCacheMixin
    from reyn.chat.tui.widgets.slash_picker import SlashPicker

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        async def _h(s, a):
            return None
        cmd = SlashCommand(name="mxn", summary="mixin probe", handler=_h)
        picker.set_matches([cmd])
        await pilot.pause()
        text = picker.rendered_text()
        assert isinstance(text, str)
        assert "/mxn" in text
        assert isinstance(picker, RenderableCacheMixin)
