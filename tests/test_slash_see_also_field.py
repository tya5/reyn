"""Tier 2: SlashCommand.see_also field + /help <cmd> focus-mode footer link.

Covers the surface added in Wave-12 T1-2:
- SlashCommand.see_also defaults to empty tuple (backward-compat).
- @slash(see_also=(...)) stores the tuple on the registered command.
- _render_command_focus renders ``  see also: <paths>`` when non-empty.
- _render_command_focus OMITS the line when see_also is empty.
- At least one populated command (/plan) has the expected see_also at
  import time.

Policy compliance:
- No MagicMock / AsyncMock / patch — real instances throughout.
- Each docstring's first line declares the Tier.
- Uses only public surfaces (SlashCommand fields, REGISTRY.get,
  _render_command_focus return value).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_slash_command_defaults_see_also_to_empty_tuple() -> None:
    """Tier 2: SlashCommand.see_also defaults to () — existing commands unaffected."""
    from reyn.slash import SlashCommand

    async def _h(s: object, a: str) -> None:
        pass

    cmd = SlashCommand(name="xtest_no_see_also", summary="test", handler=_h)
    assert cmd.see_also == ()
    assert isinstance(cmd.see_also, tuple)


def test_slash_decorator_stores_see_also_on_registered_command() -> None:
    """Tier 2: @slash(see_also=(...)) stores the tuple on the registered command."""
    from reyn.slash import REGISTRY, SlashCommand, slash

    @slash(
        "xtest_see_also_reg",
        summary="decorator see_also test",
        see_also=("docs/concepts/multi-agent/plan-mode.md", "docs/concepts/data-retrieval/memory.md"),
    )
    async def _xtest_cmd(session: object, args: str) -> None:
        pass

    cmd = REGISTRY.get("xtest_see_also_reg")
    assert cmd is not None
    assert cmd.see_also == ("docs/concepts/multi-agent/plan-mode.md", "docs/concepts/data-retrieval/memory.md")


def test_render_command_focus_includes_see_also_line_when_non_empty() -> None:
    """Tier 2: focus panel renders 'see also:' footer when see_also is non-empty."""
    from reyn.slash import REGISTRY, SlashCommand
    from reyn.slash.help import _render_command_focus

    async def _h(s: object, a: str) -> None:
        pass

    REGISTRY.register(SlashCommand(
        name="xtest_see_also_render",
        summary="render see_also test",
        handler=_h,
        see_also=("docs/concepts/multi-agent/plan-mode.md",),
    ))
    panel = _render_command_focus("xtest_see_also_render")
    assert "see also:" in panel
    assert "docs/concepts/multi-agent/plan-mode.md" in panel


def test_render_command_focus_omits_see_also_line_when_empty() -> None:
    """Tier 2: focus panel omits 'see also:' footer when see_also is empty."""
    from reyn.slash import REGISTRY, SlashCommand
    from reyn.slash.help import _render_command_focus

    async def _h(s: object, a: str) -> None:
        pass

    REGISTRY.register(SlashCommand(
        name="xtest_no_see_also_render",
        summary="omit see_also when empty",
        handler=_h,
        # see_also intentionally not set → defaults to ()
    ))
    panel = _render_command_focus("xtest_no_see_also_render")
    # No "see also:" line should appear.
    assert not any(
        line.lstrip().startswith("see also:") for line in panel.split("\n")
    )


def test_plan_command_has_expected_see_also_at_import() -> None:
    """Tier 2: /plan has see_also=('docs/concepts/multi-agent/plan-mode.md',) at import time."""
    from reyn.slash import REGISTRY  # noqa: F401 — triggers registration

    cmd = REGISTRY.get("plan")
    assert cmd is not None
    assert "docs/concepts/multi-agent/plan-mode.md" in cmd.see_also
