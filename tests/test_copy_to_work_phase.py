"""Tier 2: copy_to_work phase definition invariants (G2 preprocessor fix).

Asserts that the phase DSL encodes the deterministic preprocessor contract
that replaced the LLM-driven copy loop (B4-H2 / B5-M3 root cause: LLM
skipped write steps, leaving the workspace uncreated).

These are OS-level invariants: they verify the phase *definition* (DSL
frontmatter), not LLM behaviour. No LLM call is made; no mocks are needed.

Old tests (max_act_turns >= 5, instruction text checks) were pinning the
LLM-driven implementation and are superseded by:
  - test_copy_to_work_preprocessor.py — functional invariants
  - this file — DSL structural invariants for the preprocessor contract
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PHASE_PATH = (
    Path(__file__).parent.parent
    / "src/reyn/stdlib/skills/skill_improver/phases/copy_to_work.md"
)


def _parse_frontmatter(text: str) -> dict:
    """Minimal frontmatter parser (mirrors compiler/parser.py logic)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    if end is None:
        return {}
    return yaml.safe_load("\n".join(lines[1:end])) or {}


@pytest.fixture(scope="module")
def phase_text() -> str:
    return PHASE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(phase_text: str) -> dict:
    return _parse_frontmatter(phase_text)


def test_max_act_turns_is_zero(frontmatter):
    """Tier 2: copy_to_work max_act_turns must be 0 (G2 preprocessor fix).

    The copy is now fully deterministic via the preprocessor chain.
    No LLM act turns are needed. Setting max_act_turns=0 prevents any
    LLM invocation in the copy phase — the workspace is set up before the
    LLM's decide turn.
    """
    turns = frontmatter.get("max_act_turns")
    assert turns is not None, "copy_to_work.md must declare max_act_turns"
    assert int(turns) == 0, (
        f"copy_to_work max_act_turns must be 0 — the phase is now purely "
        f"preprocessor-driven with no LLM act turns (G2); got {turns}"
    )


def test_phase_has_preprocessor_steps(frontmatter):
    """Tier 2: copy_to_work must declare a non-empty preprocessor chain (G2).

    The glob / read / write pipeline that was previously LLM-driven is now
    implemented as deterministic preprocessor steps. An empty preprocessor
    would regress to LLM-driven behavior (B4-H2 / B5-M3).
    """
    preprocessor = frontmatter.get("preprocessor") or []
    assert isinstance(preprocessor, list), "preprocessor must be a list"
    assert len(preprocessor) > 0, (
        "copy_to_work.md must have a non-empty preprocessor chain (G2 fix)"
    )


def test_preprocessor_has_python_steps(frontmatter):
    """Tier 2: copy_to_work preprocessor must include python steps (G2).

    Python steps perform deterministic path computation and copy validation,
    which is the core of the G2 fix. Their absence would indicate the
    preprocessor chain is incomplete.
    """
    preprocessor = frontmatter.get("preprocessor") or []
    python_steps = [s for s in preprocessor if isinstance(s, dict) and s.get("type") == "python"]
    assert len(python_steps) >= 1, (
        "copy_to_work.md preprocessor must include at least one python step "
        "for deterministic path computation (G2 fix)"
    )


def test_preprocessor_has_iterate_steps(frontmatter):
    """Tier 2: copy_to_work preprocessor must include iterate steps (G2).

    Iterate steps fan the file read and write operations across all source
    files deterministically, replacing the LLM's unreliable multi-turn loop.
    """
    preprocessor = frontmatter.get("preprocessor") or []
    iterate_steps = [s for s in preprocessor if isinstance(s, dict) and s.get("type") == "iterate"]
    assert len(iterate_steps) >= 1, (
        "copy_to_work.md preprocessor must include at least one iterate step "
        "for fanning file operations across source files (G2 fix)"
    )


def test_allowed_ops_is_empty(frontmatter):
    """Tier 2: copy_to_work must declare allowed_ops: [] (G2).

    With max_act_turns=0, the LLM goes directly to the decide turn and emits
    no Control IR ops. Declaring an empty allowed_ops enforces this at the
    OS level and prevents accidental op emission.
    """
    allowed_ops = frontmatter.get("allowed_ops")
    assert allowed_ops is not None, "copy_to_work.md must declare allowed_ops"
    assert allowed_ops == [], (
        f"copy_to_work.md allowed_ops must be [] (no LLM ops in decide-only phase); "
        f"got {allowed_ops}"
    )
