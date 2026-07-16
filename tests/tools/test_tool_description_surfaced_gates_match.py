"""Tier 1: a ToolDescription's `surfaced` gate claim matches the real ToolGates.

Motivated by #2996: `descriptions/hooks.py`'s `hooks_add.surfaced` claimed
`gates.phase=allow` while the registered `ToolGates` was `phase="deny"` — a
wrong claim in LLM-facing review-aid text, caught by a human, not a test.
`surfaced` is free-text prose (`ToolDescription.surfaced: str`,
`descriptions/_types.py`); nothing previously cross-checked its
`gates.router=`/`gates.phase=` tokens against the tool's actual, registered
`ToolGates`. This test closes that gap structurally: it derives every
(claim, actual) pair from the real registry and description package, not
from a hand-picked subset.

Entries whose `surfaced` makes no gate claim at all (e.g. a description
variant explicitly documented as "NOT currently wired into any
ToolDefinition") are skipped — this test asserts CONSISTENCY of a claim
that's made, not that every entry must make one.
"""
from __future__ import annotations

import re

from reyn.tools import get_default_registry
from reyn.tools.descriptions import ALL as ALL_DESCRIPTIONS

_GATE_CLAIM_RE = re.compile(r"gates\.(router|phase)=(allow|deny)")


def test_surfaced_gate_claims_match_registered_tool_gates() -> None:
    """Tier 1: every `gates.router=`/`gates.phase=` token in `surfaced` matches reality.

    Derives the actual `ToolGates` per tool from `get_default_registry()` (the
    same construction path production uses to assemble the LLM-facing
    `tools=[...]` payload) and cross-checks it against every claim token found
    in `ALL_DESCRIPTIONS`'s `surfaced` strings — not a grepped subset of
    `descriptions/`, the full registry-backed dict.
    """
    registry = get_default_registry()
    gates_by_name = {t.name: t.gates for t in registry}

    checked = 0
    for key, desc in ALL_DESCRIPTIONS.items():
        claims = dict(_GATE_CLAIM_RE.findall(desc.surfaced))
        if not claims:
            continue  # no gate claim in this entry's surfaced text — nothing to check

        gates = gates_by_name.get(desc.tool_name)
        assert gates is not None, (
            f"descriptions.ALL[{key!r}] claims gates for tool_name="
            f"{desc.tool_name!r}, but no such tool is registered"
        )

        for axis, claimed in claims.items():
            actual = getattr(gates, axis)
            assert claimed == actual, (
                f"descriptions.ALL[{key!r}].surfaced claims gates.{axis}="
                f"{claimed!r} for tool {desc.tool_name!r}, but the registered "
                f"ToolGates has {axis}={actual!r} — surfaced={desc.surfaced!r}"
            )
        checked += 1

    assert checked > 0, "no surfaced entry made a gate claim — the regex is probably wrong"
