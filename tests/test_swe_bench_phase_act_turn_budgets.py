"""Tier 2: OS invariant — swe_bench per-phase max_act_turns budgets are wired
end-to-end through the compile pipeline (FP-0008 PR-L).

Verifies that the per-phase budget declared in each swe_bench phase frontmatter
(`max_act_turns: N`) is loaded by the compiler and exposed on the compiled
`Skill.phases[<name>].max_act_turns` — NOT the global default of 10.

No mocks.  Uses real load_dsl_skill on the installed on-disk files.
No private-state assertions.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.compiler.loader import load_dsl_skill
from reyn.schemas.models import Skill

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

_SKILL_MD = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench" / "skill.md"
)
_SKILL_ROOT = _SKILL_MD.parent.parent.parent  # src/reyn/stdlib/


def _load() -> Skill:
    """Load the swe_bench skill via the full compile pipeline."""
    return load_dsl_skill(_SKILL_MD, skill_root=_SKILL_ROOT)


# ---------------------------------------------------------------------------
# Expected per-phase budgets (= the hypothesis declared in phase frontmatter)
# ---------------------------------------------------------------------------

_EXPECTED_BUDGETS: dict[str, int] = {
    "setup":   5,
    "explore": 20,
    "plan":    15,
    "apply":   30,
    "verify":  30,
    "report":  10,
}

_GLOBAL_DEFAULT = 10


# ---------------------------------------------------------------------------
# Test 1: each phase has the expected per-phase budget (not the global default)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase_name,expected_budget", sorted(_EXPECTED_BUDGETS.items()))
def test_per_phase_budget_matches_frontmatter(phase_name: str, expected_budget: int):
    """Tier 2: Skill.phases[<phase>].max_act_turns == declared frontmatter value.

    Guards that the per-phase `max_act_turns` frontmatter key survives the full
    compile pipeline (parser → expander → Skill model).  A failure here means
    the loader silently dropped the budget declaration and would fall back to
    the global default (10), leaving the LLM under-budgeted for complex phases.
    """
    skill = _load()
    phase = skill.phases[phase_name]
    assert phase.max_act_turns == expected_budget, (
        f"Phase '{phase_name}': expected max_act_turns={expected_budget}, "
        f"got {phase.max_act_turns}. "
        f"Check frontmatter in phases/{phase_name}.md."
    )


# ---------------------------------------------------------------------------
# Test 2: all phases with budgets != global default are not accidentally 10
# ---------------------------------------------------------------------------

def test_no_phase_silently_reverts_to_global_default():
    """Tier 2: None of the 6 swe_bench phases silently inherit the global default.

    The global default (10) is insufficient for complex phases like apply and
    verify.  This test pins that every phase has an explicit, intentional budget
    that differs from what the executor would supply if max_act_turns were 0.

    Note: report is intentionally set to 10 (same value as global default) but
    the mechanism is still exercised — the frontmatter declares it explicitly,
    so the value is not a silent fallback but a deliberate choice.
    """
    skill = _load()
    phases_without_budget = [
        name
        for name, phase in skill.phases.items()
        if phase.max_act_turns == 0
    ]
    assert phases_without_budget == [], (
        f"These phases have max_act_turns=0 (= compiler default for 'not declared'): "
        f"{phases_without_budget}. Add explicit max_act_turns to their frontmatter."
    )


# ---------------------------------------------------------------------------
# Test 3: heavy phases (apply + verify) exceed the global default significantly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase_name", ["apply", "verify"])
def test_heavy_phases_exceed_global_default(phase_name: str):
    """Tier 2: apply and verify phases have budgets > global default (10).

    These phases require multiple file edits, shell ops, pytest runs, and
    parse-and-retry loops.  A budget <= 10 is empirically insufficient
    (sandbox_2 v7 calibration: 4 of 5 aborts hit remaining_act_turns=0).
    """
    skill = _load()
    budget = skill.phases[phase_name].max_act_turns
    assert budget > _GLOBAL_DEFAULT, (
        f"Phase '{phase_name}': budget {budget} must exceed global default "
        f"{_GLOBAL_DEFAULT} to handle SWE-bench task complexity."
    )


# ---------------------------------------------------------------------------
# Test 4: setup phase has a budget <= global default (lightweight phase)
# ---------------------------------------------------------------------------

def test_setup_phase_is_lightweight():
    """Tier 2: setup phase budget <= global default (5 turns is sufficient).

    setup only does `git checkout`, `git status`, and `pytest --version` —
    three shell ops at most.  A budget significantly above 10 would be wasteful
    and mask runaway behaviour.
    """
    skill = _load()
    budget = skill.phases["setup"].max_act_turns
    assert budget <= _GLOBAL_DEFAULT, (
        f"Phase 'setup': budget {budget} should be <= {_GLOBAL_DEFAULT} "
        f"(setup is a lightweight phase doing only git checkout + verify)."
    )
