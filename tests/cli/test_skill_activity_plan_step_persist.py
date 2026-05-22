"""Tier 2: SkillActivityRow persists the plan-step badge across set_phase
and subsequent set_detail calls.

The forwarder emits ``"plan N/M"`` exactly once per spawned sub-skill
``run_id`` (see ``ChatEventForwarder.on_phase_started`` + de-dup via
``_plan_step_announced``). Without this contract, the SkillActivityRow
would lose the plan attribution after the first in-phase signal
(on_llm_called / on_act_executed → ``set_detail("llm: …")``) or after
the next phase advance (``set_phase`` clears ``_detail``).

These tests pin that the persistent ``_plan_step_label`` slot survives:

1. A subsequent ``set_detail`` with a non-plan payload (= llm: / act:).
2. A ``set_phase`` call that clears the ephemeral ``_detail`` slot.
3. The badge appears in the rendered running line so the user can see
   "this sub-skill is plan step 2/5" at any time during execution.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _row():
    """Construct a SkillActivityRow without mounting it.

    ``_refresh`` returns early when ``_static is None`` (= not composed),
    so methods that drive internal state can be exercised directly
    against the row's public API without requiring a Textual app
    context. The rendering helpers (``_build_running``) are pure
    over the internal fields and can be called the same way.
    """
    from reyn.chat.tui.widgets.skill_activity import SkillActivityRow
    return SkillActivityRow(run_id="abc1efgh", skill_name="test_skill")


def test_plan_step_label_persists_through_in_phase_set_detail() -> None:
    """Tier 2: ``set_detail("plan N/M")`` populates the persistent slot,
    and a follow-up ``set_detail("llm: …")`` does NOT clobber it.
    """
    row = _row()
    row.set_detail("plan 2/5")
    assert row._plan_step_label == "plan 2/5"
    assert row._detail == ""
    row.set_detail("llm: opus-4-7")
    # ephemeral detail updated …
    assert row._detail == "llm: opus-4-7"
    # … but the persistent plan badge stays put.
    assert row._plan_step_label == "plan 2/5"


def test_plan_step_label_survives_set_phase() -> None:
    """Tier 2: ``set_phase`` clears ``_detail`` but leaves the plan badge."""
    row = _row()
    row.set_detail("plan 3/7")
    row.set_detail("act: 2 ops")
    assert row._detail == "act: 2 ops"
    assert row._plan_step_label == "plan 3/7"
    row.set_phase("execute", visit=1)
    # phase advance clears in-phase detail …
    assert row._detail == ""
    # … but the plan attribution remains visible.
    assert row._plan_step_label == "plan 3/7"


def test_plan_step_label_renders_in_running_line() -> None:
    """Tier 2: the persistent badge appears in the running render output.

    The exact wrapping (``[`` / ``]``) is not pinned — only that the
    badge text surfaces somewhere in the running Text. Tolerates
    future renderer adjustments (different separator, color, etc.)
    so long as the badge is observable to the user.
    """
    row = _row()
    row.set_phase("resolve")
    row.set_detail("plan 4/9")
    rendered = row._build_running().plain
    assert "plan 4/9" in rendered


def test_non_plan_detail_does_not_populate_plan_label() -> None:
    """Tier 2: regular details (= llm: / act:) stay in the ephemeral slot.

    Only payloads matching ``plan N/M`` shape land in the persistent
    slot — everything else uses the existing ephemeral routing so
    in-phase signals keep their normal "replaced each event" semantics.
    """
    row = _row()
    row.set_detail("llm: opus")
    assert row._plan_step_label == ""
    assert row._detail == "llm: opus"
    row.set_detail("act: 3 ops")
    assert row._plan_step_label == ""
    assert row._detail == "act: 3 ops"
