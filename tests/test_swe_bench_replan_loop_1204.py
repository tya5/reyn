"""Tier 2: OS/skill invariant — swe_bench verify→plan re-plan iteration loop
(#1204, deferred from #1203).

Pins the re-plan loop wiring + the not_locatable propagation that feeds it:
  - Graph: `verify` routes to BOTH `report` and `plan` (re-plan reachable); the
    previously-unsatisfiable `apply → plan` edge is removed (apply emits
    apply_state, plan accepts exploration|verify_state only — a dead edge).
  - `apply_state` carries a `not_locatable` field (the anchors the apply
    preprocessor dropped); apply.md instructs carrying them; verify.md MUST
    append them to `failure_summary` on failure so a re-plan avoids reissuing
    the same unlocatable edit; plan increments `attempt` so the verify retry
    limit bounds the loop.

NOTE on determinism: the carry (apply→apply_state) and the fold
(verify→failure_summary) are LLM-mediated — there is no per-phase deterministic
postprocessor in the OS (postprocessor is skill-level only; schemas/models.py).
The full deterministic upgrade is tracked separately. These are faithful-copy
instructions over already-visible data (degraded-not-broken on omission, bounded
by the 3-attempt limit), a different class from the #1216 hallucination break —
so the behavioral fold is verified by a light dogfood, and these Tier-2 tests
pin the deterministic substrate (graph edges, schema field, instruction
presence, P1/P8 cleanliness) that the behavior depends on.

No mocks. Uses real load_dsl_skill + on-disk phase-file reads.
"""
from __future__ import annotations

from pathlib import Path

from reyn.compiler.loader import load_dsl_skill
from reyn.schemas.models import Skill

_SKILL_DIR = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)
_SKILL_MD = _SKILL_DIR / "skill.md"
_SKILL_ROOT = _SKILL_MD.parent.parent.parent  # src/reyn/stdlib/
_APPLY_MD = _SKILL_DIR / "phases" / "apply.md"
_VERIFY_MD = _SKILL_DIR / "phases" / "verify.md"
_PLAN_MD = _SKILL_DIR / "phases" / "plan.md"
_APPLY_STATE = _SKILL_DIR / "artifacts" / "apply_state.yaml"


def _load() -> Skill:
    return load_dsl_skill(_SKILL_MD, skill_root=_SKILL_ROOT)


# ── graph: re-plan edge added, dead apply→plan edge removed ──────────────────


def test_verify_routes_to_report_and_plan() -> None:
    """Tier 2: verify's candidates include BOTH report and plan (re-plan reachable)."""
    skill = _load()
    verify_targets = skill.graph.transitions.get("verify", [])
    assert "report" in verify_targets, "verify must still reach report (terminal)"
    assert "plan" in verify_targets, (
        "verify must reach plan for the re-plan loop (the #1204 edge). "
        f"got: {verify_targets}"
    )


def test_apply_to_plan_dead_edge_removed() -> None:
    """Tier 2: the unsatisfiable apply→plan edge is removed (apply only reaches verify).

    apply emits `apply_state`; plan accepts only `exploration | verify_state`, so
    `apply → plan` could never satisfy plan's input_schema — a dead edge. The
    re-plan loop runs through verify→plan; an all-not-locatable apply still
    produces a (partial) patch → verify → re-plan.
    """
    skill = _load()
    apply_targets = skill.graph.transitions.get("apply", [])
    assert "plan" not in apply_targets, (
        f"the dead apply→plan edge must be removed; got: {apply_targets}"
    )
    assert "verify" in apply_targets


# ── not_locatable propagation substrate (schema + instructions) ──────────────


def test_apply_state_schema_carries_not_locatable() -> None:
    """Tier 2: apply_state declares a `not_locatable` field (the re-plan input signal)."""
    text = _APPLY_STATE.read_text(encoding="utf-8")
    assert "not_locatable" in text, (
        "apply_state must declare not_locatable so the dropped anchors can flow to a re-plan."
    )


def test_apply_md_carries_not_locatable() -> None:
    """Tier 2: apply.md instructs carrying the dropped anchors into the output."""
    text = _APPLY_MD.read_text(encoding="utf-8").lower()
    assert "not_locatable" in text and "anchor" in text, (
        "apply.md Step 4 must instruct preserving the not_locatable anchors in the output."
    )


def test_verify_md_must_append_not_locatable_to_failure_summary() -> None:
    """Tier 2: verify.md MUST-append the not_locatable anchors into failure_summary on failure."""
    text = _VERIFY_MD.read_text(encoding="utf-8")
    assert "not_locatable" in text, "verify.md must reference the not_locatable input signal."
    lowered = text.lower()
    assert "append" in lowered and "failure_summary" in lowered, (
        "verify.md must instruct appending the not_locatable anchors to failure_summary "
        "(append-pattern, preserving the test-failure diagnosis)."
    )


def test_verify_md_has_replan_outcome_guidance() -> None:
    """Tier 2: verify.md steers toward re-planning while attempts remain (P1/P8: no phase name)."""
    lowered = _VERIFY_MD.read_text(encoding="utf-8").lower()
    assert "below the maximum" in lowered or "attempts remain" in lowered, (
        "verify.md must describe the tests-failed-and-attempts-remain outcome so the OS can "
        "offer the re-plan candidate."
    )


def test_plan_md_increments_attempt_for_bounding() -> None:
    """Tier 2: plan.md instructs incrementing attempt so the retry limit bounds the loop."""
    lowered = _PLAN_MD.read_text(encoding="utf-8").lower()
    assert "attempt" in lowered and "+ 1" in lowered, (
        "plan.md must instruct attempt = verify_state.attempt + 1 so the loop is bounded "
        "by the verify retry limit (an un-incremented attempt loops unbounded)."
    )

# (P1/P8 "verify.md names no transition phase" is enforced structurally by the OS
# — a phase only ever picks from OS-offered candidates, never a hardcoded target —
# and is covered behaviorally by the graph-transition tests above; a string-absence
# grep on the .md is a format-pin, so it is intentionally not asserted here.)
