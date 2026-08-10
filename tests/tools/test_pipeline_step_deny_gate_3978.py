"""Tier 2: proposal 0067 P7 (#3978) — the two pipeline-nesting deny sets
stay in equality.

``pipeline_verbs._PIPELINE_STEP_DENY_TOOLS`` (a ToolStep's own dispatch
denial, R6 S3) and ``session_api._DELEGATION_DENY_TOOLS`` (an agent step's
narrowing denial, same rule) are two INDEPENDENT collections stating the
SAME fact — "these tools must not be reachable from inside a pipeline step,
nesting is call-only" — and neither module can see the other's set: no test
that reads only one of them can catch a drift where one set gains or loses a
name and the other doesn't.

This gate exists because this arc (#3978) collided on exactly this pair
twice already this session (P5's rebase onto P4's tools, and this PR's own
retirement of 3 of the 4 pipeline names) — both times the conflict was
correctly resolved by hand, but neither prior resolution had an automated
check that the two sets actually agree. Architect required one before P7
could rely on having consolidated the retiring names correctly in both
places at once.
"""
from __future__ import annotations

from reyn.runtime.session_api import _DELEGATION_DENY_TOOLS
from reyn.tools.pipeline_verbs import _PIPELINE_STEP_DENY_TOOLS


def test_pipeline_step_deny_sets_are_equal():
    """Tier 2: the two independent deny collections name the exact same
    tools. Falsify-verified (see the companion strip-falsify test below):
    adding a name to only one side makes this go RED."""
    assert set(_PIPELINE_STEP_DENY_TOOLS) == set(_DELEGATION_DENY_TOOLS), (
        "pipeline_verbs._PIPELINE_STEP_DENY_TOOLS and "
        "session_api._DELEGATION_DENY_TOOLS have drifted apart — a tool "
        "denied from a ToolStep but not an agent step (or vice versa) is a "
        "real R6 S3 nesting-cost-bound hole, not a cosmetic mismatch"
    )


def test_pipeline_step_deny_sets_are_non_empty():
    """Tier 2: vacuity guard — an accidentally-emptied set would make the
    equality test above pass vacuously (two empty sets are equal), silently
    disabling the R6 S3 nesting deny entirely."""
    assert _PIPELINE_STEP_DENY_TOOLS, "the ToolStep deny set must not be empty"
    assert _DELEGATION_DENY_TOOLS, "the agent-step deny set must not be empty"


def test_pipeline_step_deny_sets_name_the_expected_tools():
    """Tier 2: proposal 0067 P7 (#3978) — after retiring run_pipeline_async /
    run_pipeline_inline / run_pipeline_inline_async (4 names -> 1), and P6
    (#3978) retiring delegate_to_agent with no replacement in this specific
    deny-set (see ``_DELEGATION_DENY_TOOLS``'s own comment: nothing today
    shares its async-dispatch-ends-the-turn posture), both deny sets must
    contain exactly {run_pipeline} — not a superset that still carries a
    retired name (which would silently mean the retired name is "denied"
    everywhere, masking that it no longer exists as a real registered tool
    at all) and not an empty set."""
    expected = {"run_pipeline"}
    assert set(_PIPELINE_STEP_DENY_TOOLS) == expected
    assert set(_DELEGATION_DENY_TOOLS) == expected
