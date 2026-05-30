"""Tier 2: palette distinction invariants — semantic tokens must stay pairwise distinct.

Guards the substitute-vs-orthogonal colour distinctions in the TUI palette.
A future "palette cleanup" that collapses any of these pairs would silently
erase a designed visual distinction (= CI-invisible regression). This file
makes the regression visible at CI time.

What this IS: an invariant test — it asserts that semantically separate
concepts remain visually separable (distinct hex values). The assertions
pin DISTINCTNESS, not specific hex strings.

What this is NOT: a format-pin test. It does not assert on specific colour
values, lengths, or sizes. The != assertions below are inequality checks
on Python string values, not format / shape pins.

Rationale for each pair:
  - _STATUS_ERROR != _STATUS_CRITICAL: recoverable failures (tool errors,
    validation failures, retry-able events) vs hard-stop events (permission
    denied, budget exceeded, phase_failed) are distinct severity tiers. The
    eye needs a visual cue to tell "retry this" from "session is halted".
  - _STATUS_SUCCESS != _STATUS_ERROR: success / failure must be
    distinguishable from each other -- the baseline correctness invariant for
    any status indicator.
  - _EVENT_PLAN != _EVENT_SKILL: plan events (orange family) are a
    separate forensic category from skill-run events (blue family). Collapsing
    them would erase the ability to scan the events tab and quickly identify
    which lines belong to plan lifecycle vs. skill execution.
  - _EVENT_TOOL != _EVENT_SKILL: tool invocations (purple) are distinct
    from skill orchestration (blue). A developer reading the events pane
    needs to distinguish "a tool ran" from "a skill ran".
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui._palette import (
    _EVENT_PLAN,
    _EVENT_SKILL,
    _EVENT_TOOL,
    _STATUS_CRITICAL,
    _STATUS_ERROR,
    _STATUS_SUCCESS,
)


def test_status_error_distinct_from_status_critical() -> None:
    """Tier 2: recoverable-error vs hard-stop severity tiers must stay visually separable.

    _STATUS_ERROR (recoverable: tool failure, validation error, mcp
    timeout, retry-able) and _STATUS_CRITICAL (hard stop: permission
    denied, phase_failed, budget exceeded, interrupted) are designed as
    separate severity tiers. If they collapse to the same colour the user
    loses the visual cue distinguishing "retry-able" from "session halted".
    """
    assert _STATUS_ERROR != _STATUS_CRITICAL, (
        f"_STATUS_ERROR and _STATUS_CRITICAL must be distinct; "
        f"got both = {_STATUS_ERROR!r}. "
        "Collapsing these erases the recoverable-vs-hard-stop severity distinction."
    )


def test_status_success_distinct_from_status_error() -> None:
    """Tier 2: success and error indicators must be visually distinguishable.

    Baseline correctness invariant -- a status indicator that makes success
    and failure look the same provides no signal.
    """
    assert _STATUS_SUCCESS != _STATUS_ERROR, (
        f"_STATUS_SUCCESS and _STATUS_ERROR must be distinct; "
        f"got both = {_STATUS_SUCCESS!r}. "
        "A status system where success == error provides no information."
    )


def test_event_plan_distinct_from_event_skill() -> None:
    """Tier 2: plan-event orange and skill-event blue must be visually separable.

    _EVENT_PLAN (plan emitted/aggregated/timeout, orange family) vs
    _EVENT_SKILL (skill_run/workflow/artifact/index activity, blue family)
    are different forensic categories in the events tab. The orange plan family
    gives the operator a quick visual scan path for plan lifecycle across mixed
    event streams. Collapsing to a single colour removes that scan affordance.
    """
    assert _EVENT_PLAN != _EVENT_SKILL, (
        f"_EVENT_PLAN and _EVENT_SKILL must be distinct; "
        f"got both = {_EVENT_PLAN!r}. "
        "Plan orange vs skill blue is a designed forensic separation in the events tab."
    )


def test_event_tool_distinct_from_event_skill() -> None:
    """Tier 2: tool-invocation purple and skill-orchestration blue must be separable.

    _EVENT_TOOL (tool / mcp invocation, purple) vs _EVENT_SKILL
    (skill orchestration, blue) appear in the same events tab stream.
    A developer debugging a plan needs to distinguish "a tool ran" from
    "a skill ran" without hovering over each row. Collapsing them
    removes that immediate scan affordance.
    """
    assert _EVENT_TOOL != _EVENT_SKILL, (
        f"_EVENT_TOOL and _EVENT_SKILL must be distinct; "
        f"got both = {_EVENT_TOOL!r}. "
        "Tool purple vs skill blue is a designed forensic separation."
    )
