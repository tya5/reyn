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

from reyn.tui._palette import (
    _EVENT_PLAN,
    _EVENT_PLAN_STEP,
    _EVENT_SKILL,
    _EVENT_TOOL,
    _HINT_ACTION,
    _RED_MUTED,
    _STATUS_CRITICAL,
    _STATUS_ERROR,
    _STATUS_READY,
    _STATUS_SUCCESS,
    _STATUS_SUCCESS_DARK,
    _STATUS_SUCCESS_DIM,
    _STATUS_WARN,
    _TEXT_DIM,
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


def test_status_warn_distinct_from_near_ambers() -> None:
    """Tier 2: warning amber must stay distinct from the near-collision amber tokens.

    _STATUS_WARN (#ffaa44 — "taking a while / pending / stalled / cap-proximity")
    sits one hue-step from _EVENT_PLAN_STEP (#ffaa66, plan-step lifecycle): two
    chars apart in hex. They name different concepts (a transient warning vs a
    plan-step event category) and a careless "merge the ambers" cleanup would
    erase that. Also assert it stays off the error tiers — a warning is not an
    error.
    """
    assert _STATUS_WARN != _EVENT_PLAN_STEP, (
        f"_STATUS_WARN and _EVENT_PLAN_STEP must be distinct (near collision); "
        f"got _STATUS_WARN={_STATUS_WARN!r}, _EVENT_PLAN_STEP={_EVENT_PLAN_STEP!r}. "
        "Warning amber vs plan-step amber are different concepts."
    )
    assert _STATUS_WARN != _STATUS_ERROR, "warning is not a (recoverable) error"
    assert _STATUS_WARN != _STATUS_CRITICAL, "warning is not a hard-stop"


def test_status_success_dark_distinct_from_brighter_greens() -> None:
    """Tier 2: the dim cost-figure green must stay separable from the brighter success greens.

    _STATUS_SUCCESS_DARK (#2d7a4f — low-emphasis cost figures in cost_tab) is
    the darkest of a 3-step success-green ramp: _STATUS_SUCCESS (#44cc88,
    completed/running) and _STATUS_SUCCESS_DIM (#88ddaa, lighter sibling). The
    dark one reads as "nominal background detail" without competing with the
    brighter greens; collapsing any pair would flatten that emphasis ramp.
    """
    assert _STATUS_SUCCESS_DARK != _STATUS_SUCCESS, (
        f"_STATUS_SUCCESS_DARK and _STATUS_SUCCESS must be distinct; "
        f"got both = {_STATUS_SUCCESS_DARK!r}."
    )
    assert _STATUS_SUCCESS_DARK != _STATUS_SUCCESS_DIM, (
        f"_STATUS_SUCCESS_DARK and _STATUS_SUCCESS_DIM must be distinct; "
        f"got both = {_STATUS_SUCCESS_DARK!r}."
    )


def test_status_ready_distinct_from_status_success() -> None:
    """Tier 2: the "ready" olive must stay separable from the "running/done" green.

    _STATUS_READY (#aaaa55, olive — agent/process loaded-but-idle) vs
    _STATUS_SUCCESS (#44cc88, green — running/completed) appear side by side
    in the agents + pending panels (◐ ready vs ● running). If they collapse the
    operator can no longer tell at a glance which agents are working.
    """
    assert _STATUS_READY != _STATUS_SUCCESS, (
        f"_STATUS_READY and _STATUS_SUCCESS must be distinct; "
        f"got both = {_STATUS_READY!r}. Ready-olive vs running-green is a "
        "designed at-a-glance distinction in the agents/pending panels."
    )


def test_red_muted_distinct_from_error_tiers() -> None:
    """Tier 2: the muted "soft negative" red must stay off the active-error reds.

    _RED_MUTED (#aa6666 — cancelled / partial-reply aborted / remote-limited /
    8-colour error-glyph fallback) is a deliberately desaturated red for
    "soft negative" states. It must stay distinct from _STATUS_ERROR (#ff6644,
    an active recoverable failure) and _STATUS_CRITICAL (#ff4444, a hard stop):
    a user-initiated cancel must not read as loud as a real failure.
    """
    assert _RED_MUTED != _STATUS_ERROR, (
        f"_RED_MUTED and _STATUS_ERROR must be distinct; "
        f"got _RED_MUTED={_RED_MUTED!r}, _STATUS_ERROR={_STATUS_ERROR!r}. "
        "A cancel/soft-negative must not look like an active failure."
    )
    assert _RED_MUTED != _STATUS_CRITICAL, (
        f"_RED_MUTED and _STATUS_CRITICAL must be distinct; "
        f"got _RED_MUTED={_RED_MUTED!r}, _STATUS_CRITICAL={_STATUS_CRITICAL!r}."
    )


def test_hint_action_distinct_from_metadata_dim() -> None:
    """Tier 2: the actionable inline hint must stay separable from dim metadata.

    _HINT_ACTION (#8a7a4a, muted gold — the ErrorBox extracted recovery hint,
    "do this") vs _TEXT_DIM (#555555 — the metadata eb-hint, "Ctrl+B → events").
    The two hint rows sit adjacent; collapsing them would erase the cue that one
    is an action to take and the other is just navigation metadata.
    """
    assert _HINT_ACTION != _TEXT_DIM, (
        f"_HINT_ACTION and _TEXT_DIM must be distinct; "
        f"got _HINT_ACTION={_HINT_ACTION!r}, _TEXT_DIM={_TEXT_DIM!r}. "
        "Actionable hint vs metadata hint is a designed distinction."
    )
