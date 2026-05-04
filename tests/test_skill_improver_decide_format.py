"""Tier 2 invariant tests for skill_improver decide-turn instructions.

Pins the instruction invariants introduced in B5-M2 H1+H2+H3 fix:
- plan_improvements.md must mandate a top-level control block in decide turns
- apply_improvements.md must mandate the same control block
- apply_improvements.md must surface the single-act-turn constraint prominently
- apply_improvements.md must instruct the LLM to source all required fields
  from session._resolved_paths rather than constructing them independently

These are Tier 2 OS-invariant tests: they pin skill design invariants
(instruction content that must not silently disappear) via string-contains
assertions on the phase files.  No mocks, no private state.
"""
from __future__ import annotations

from pathlib import Path

_SKILL_DIR = (
    Path(__file__).parent.parent
    / "src"
    / "reyn"
    / "stdlib"
    / "skills"
    / "skill_improver"
    / "phases"
)


def _read(name: str) -> str:
    return (_SKILL_DIR / name).read_text(encoding="utf-8")


# ── (a) plan_improvements.md must mandate the control block ──────────────────


def test_plan_improvements_mandates_control_block():
    """Tier 2: plan_improvements.md must instruct LLM to always include a top-level control block.

    Invariant: the decide-turn format reminder introduced in B5-M2 H1 must
    remain present.  If it disappears, weak LLM is likely to omit the control
    block and trigger a phase_retry via normalizer.py:146-149.
    """
    content = _read("plan_improvements.md")
    assert "control" in content and "CRITICAL" in content, (
        "plan_improvements.md must contain a CRITICAL reminder about the top-level "
        "control block in decide turns (B5-M2 H1 fix)"
    )
    assert "OS rejects" in content, (
        "plan_improvements.md must explain that the OS rejects responses without "
        "a control block, to make the constraint concrete for the LLM"
    )


# ── (b) apply_improvements.md must mandate the control block ─────────────────


def test_apply_improvements_mandates_control_block():
    """Tier 2: apply_improvements.md must instruct LLM to always include a top-level control block.

    Invariant: the same decide-turn format reminder must be present in
    apply_improvements.md.  H1 can occur in both plan and apply phases.
    """
    content = _read("apply_improvements.md")
    assert "control" in content and "CRITICAL" in content, (
        "apply_improvements.md must contain a CRITICAL reminder about the top-level "
        "control block in decide turns (B5-M2 H1 fix)"
    )
    assert "OS rejects" in content, (
        "apply_improvements.md must explain that the OS rejects responses without "
        "a control block"
    )


# ── (c) apply_improvements.md must surface the single-act-turn constraint ────


def test_apply_improvements_surfaces_single_act_turn_constraint():
    """Tier 2: apply_improvements.md must prominently warn about the max_act_turns=1 budget.

    Invariant: the single-act-turn budget and the consequence of exceeding it
    (OS forces a decide turn immediately) must be stated early and explicitly.
    Without this, weak LLM may issue a second act turn and trigger force_decide
    retry (B5-M2 H2 fix).
    """
    content = _read("apply_improvements.md")
    assert "max_act_turns" in content, (
        "apply_improvements.md must mention max_act_turns to make the budget "
        "constraint visible to the LLM (B5-M2 H2 fix)"
    )
    # The constraint reminder must appear before the Step 1 heading so that
    # weak LLMs reading top-to-bottom see it first.
    constraint_pos = content.find("max_act_turns")
    step1_pos = content.find("## Step 1")
    assert constraint_pos < step1_pos, (
        "The max_act_turns constraint reminder must appear before '## Step 1' "
        "so weak LLMs see it before they start planning ops (B5-M2 H2 fix)"
    )


# ── (d) apply_improvements.md must instruct _resolved_paths sourcing ─────────


def test_apply_improvements_instructs_resolved_paths_sourcing():
    """Tier 2: apply_improvements.md must instruct LLM to source all required artifact fields from _resolved_paths.

    Invariant: the finalize path of apply_improvements must explicitly tell the
    LLM to read every required field from session._resolved_paths rather than
    inventing or leaving values as null.  Without this, the LLM produces None
    for path fields and triggers artifact schema validation failure (B5-M2 H3 fix).
    """
    content = _read("apply_improvements.md")
    assert "_resolved_paths" in content, (
        "apply_improvements.md must reference _resolved_paths to guide the LLM "
        "when building the finalize artifact (B5-M2 H3 fix)"
    )
    assert "null" in content or "required field" in content or "Do NOT" in content, (
        "apply_improvements.md must warn against leaving required fields null or "
        "omitted in the improvement_result artifact (B5-M2 H3 fix)"
    )
