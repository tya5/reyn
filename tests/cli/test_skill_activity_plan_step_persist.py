"""Tier 2: SkillActivityRow persists the plan-step badge across set_phase
and subsequent set_detail calls.

The forwarder emits ``"plan N/M"`` exactly once per spawned sub-skill
``run_id`` (see ``ChatEventForwarder.on_phase_started`` + de-dup via
``_plan_step_announced``). Without this contract, the SkillActivityRow
would lose the plan attribution after the first in-phase signal
(on_llm_called / on_act_executed → ``set_detail("llm: …")``) or after
the next phase advance (``set_phase`` clears the ephemeral detail).

These tests pin that the persistent plan badge surfaces in the
rendered ``_build_running`` output even after:

1. A subsequent ``set_detail`` with a non-plan payload (= llm: / act:).
2. A ``set_phase`` call that clears the ephemeral in-phase detail.
3. Non-plan detail payloads do NOT leave a stray "plan" marker behind
   in the render output.

All assertions go through the public render surface
(``row.build_running().plain``) per CLAUDE.md testing policy: "NEVER
assert on private state. Use the public surface or a snapshot()-style
read." The renderer is what the user actually sees; verifying through
the render contract keeps the test robust against future field /
naming refactors of the internal slot.
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
    context. The renderer (``_build_running``) is pure over those
    fields and can be called the same way to read the visible state.
    """
    from reyn.chat.tui.widgets.skill_activity import SkillActivityRow
    return SkillActivityRow(run_id="abc1efgh", skill_name="test_skill")


def test_plan_step_badge_persists_through_in_phase_detail() -> None:
    """Tier 2: a follow-up ``set_detail("llm: …")`` does NOT erase the
    plan badge from the rendered output.

    Sequence: plan badge arrives first → row shows "plan 2/5". The
    next in-phase signal arrives (llm: opus-4-7) → row STILL shows
    "plan 2/5" alongside the new in-phase detail.
    """
    row = _row()
    row.set_phase("resolve")
    row.set_detail("plan 2/5")
    assert "plan 2/5" in row.build_running().plain
    row.set_detail("llm: opus-4-7")
    rendered = row.build_running().plain
    assert "plan 2/5" in rendered, (
        "plan badge must survive the next in-phase set_detail call"
    )
    assert "llm: opus-4-7" in rendered, (
        "ephemeral detail still updates normally"
    )


def test_plan_step_badge_survives_set_phase() -> None:
    """Tier 2: ``set_phase`` clears the ephemeral detail but leaves the
    plan badge visible in the render.
    """
    row = _row()
    row.set_phase("resolve")
    row.set_detail("plan 3/7")
    row.set_detail("act: 2 ops")
    pre = row.build_running().plain
    assert "plan 3/7" in pre
    assert "act: 2 ops" in pre
    row.set_phase("execute", visit=1)
    post = row.build_running().plain
    assert "plan 3/7" in post, (
        "plan badge must survive set_phase clearing the ephemeral detail"
    )
    assert "act: 2 ops" not in post, (
        "ephemeral detail is cleared on phase advance, as before"
    )
    assert "execute" in post, "new phase name appears in render"


def test_plan_step_badge_appears_on_first_set_detail() -> None:
    """Tier 2: the very first ``set_detail("plan N/M")`` call surfaces
    the badge in the running render.

    Pins the entry path — without this we couldn't tell whether the
    contract was broken at routing (set_detail not recognising the
    payload) or at rendering (renderer ignoring the persistent slot).
    """
    row = _row()
    row.set_phase("resolve")
    row.set_detail("plan 4/9")
    rendered = row.build_running().plain
    assert "plan 4/9" in rendered


def test_non_plan_details_do_not_leak_plan_marker_into_render() -> None:
    """Tier 2: regular details (= llm: / act:) routed through set_detail
    do NOT cause a "plan" marker to appear in the rendered output.

    Pins the routing predicate: only payloads matching the ``plan N/M``
    shape land in the persistent slot. Other payloads stay in the
    ephemeral detail and don't bleed into the persistent render
    segment.
    """
    row = _row()
    row.set_phase("resolve")
    row.set_detail("llm: opus")
    rendered = row.build_running().plain
    assert "llm: opus" in rendered
    # No plan-step badge appears because no "plan N/M" payload was
    # routed in.
    assert "plan " not in rendered
    row.set_detail("act: 3 ops")
    rendered = row.build_running().plain
    assert "act: 3 ops" in rendered
    assert "plan " not in rendered
