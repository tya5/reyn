"""Tier 2: SkillActivityRow uses ⊘ glyph for user-cancelled state (C-F5).

Wave-8 Topic C finding F5 (P2): before this fix, a Ctrl+C-cancelled
skill rendered as ``✗ skill#abcd · failed: cancelled · Ctrl+B → events``
— visually identical to a system failure (= the same ``✗`` glyph in
bold red). Scrolling back through history, the user couldn't tell
"I cancelled this" from "the system failed this".

Now ``finish(aborted=True)`` renders ``⊘`` in dim grey + "cancelled"
text (without the events tab pointer). Same shape design as
ToolCallRow's aborted state (= color + glyph as redundant cue).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _row():
    """Construct an unmounted SkillActivityRow for direct exercise."""
    from reyn.chat.tui.widgets.skill_activity import SkillActivityRow
    return SkillActivityRow(run_id="abc1efgh", skill_name="test_skill")


def test_finish_aborted_renders_circle_slash_glyph_not_x() -> None:
    """Tier 2: aborted state renders ``⊘`` (= dim grey), NOT ``✗`` (= red).

    Color is the redundant cue alongside the glyph; tests assert
    on the rendered plain text via the existing ``_build_finished``
    public render surface.
    """
    row = _row()
    row.finish(success=False, reason="cancelled", aborted=True)
    finished = row._build_finished().plain
    assert "⊘" in finished, "aborted state must render ⊘"
    assert "✗" not in finished, "aborted state must NOT render ✗"
    # Reason text uses "cancelled:" not "failed:" for the aborted path.
    assert "cancelled" in finished


def test_finish_aborted_with_no_reason_falls_back_to_bare_cancelled() -> None:
    """Tier 2: empty reason → bare ``"cancelled"`` (no orphan colon)."""
    row = _row()
    row.finish(success=False, reason="", aborted=True)
    finished = row._build_finished().plain
    assert "⊘" in finished
    # Should NOT show ``cancelled:`` (= empty after the colon) — bare
    # ``cancelled`` is the only legible form.
    assert "cancelled:" not in finished
    assert "cancelled" in finished


def test_finish_failure_keeps_x_glyph_without_aborted_flag() -> None:
    """Tier 2: existing system-failure path unaffected — still ✗ + red.

    Ensures the aborted flag is opt-in; legacy callers that pass only
    ``success=False`` continue to get the ✗ rendering.
    """
    row = _row()
    row.finish(success=False, reason="timeout")
    finished = row._build_finished().plain
    assert "✗" in finished
    assert "⊘" not in finished
    assert "failed: timeout" in finished
    assert "Ctrl+B → events" in finished


def test_finish_success_unchanged_when_aborted_flag_ignored() -> None:
    """Tier 2: ``success=True`` ignores ``aborted`` — still ✓ rendering.

    The aborted branch only fires when ``success=False AND aborted=True``;
    a successful finish should never reach the aborted code path.
    """
    row = _row()
    row.finish(success=True, reason="2 phases", aborted=True)
    finished = row._build_finished().plain
    assert "✓" in finished
    assert "⊘" not in finished
