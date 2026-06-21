"""Tier 2: inline SkillActivityRow widget IDs are unique per run_id, not per date.

Regression: the inline row id was ``skillrow_{run_id[:8]}`` — but a run_id like
``20260621T003150717803Z_direct_llm_056f`` has ``[:8] == "20260621"`` (the
YYYYMMDD date). So two skills spawned the SAME day collapsed to the same widget
id; the second mount raised Textual ``DuplicateIds`` (swallowed by the outbox
trace handler → the second skill's inline row silently vanished + an error was
logged). The widget id must track the full run_id (= the dict key already used
for dedup in ``start_skill_row``).

Public surface only: the rendered ``SkillActivityRow`` widgets in the DOM and
their ``id`` attributes — no private dicts asserted.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_two_skills_same_day_distinct_run_ids_both_mount() -> None:
    """Tier 2: two distinct same-day run_ids mount two rows with distinct ids.

    Falsifies the date-truncation collision: ``run_id[:8]`` is identical for
    both run_ids below, so the buggy id scheme mounts the second row with a
    duplicate widget id (DuplicateIds). The fix keys the id on the full run_id.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.interfaces.tui.widgets.skill_activity import SkillActivityRow

    # Same YYYYMMDD prefix, different time + hash → run_id[:8] collides.
    rid1 = "20260621T003150717803Z_direct_llm_056f"
    rid2 = "20260621T003153486860Z_direct_llm_de29"
    assert rid1[:8] == rid2[:8] and rid1 != rid2, "collision precondition broken"

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv.start_skill_row(rid1, "direct_llm")
        conv.start_skill_row(rid2, "direct_llm")
        await pilot.pause()

        rows = list(conv.query(SkillActivityRow))
        row_count = len(rows)
        assert row_count == 2, (
            f"both same-day skills should mount a row; got {row_count}"
        )
        # The fix: distinct widget ids per run_id (buggy code reused
        # skillrow_<date> for both). Assert distinctness directly, not a
        # set-size, so this pins behaviour rather than a count.
        ids = [r.id for r in rows]
        assert ids[0] != ids[1], (
            f"skill-row widget ids must be distinct per run_id; got {ids!r}"
        )
