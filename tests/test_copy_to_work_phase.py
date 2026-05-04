"""Tier 2: copy_to_work phase definition invariants (B4-H2 + B4-L1).

Asserts that the phase DSL file encodes the budget and glob constraints
required to prevent act-turn exhaustion (B4-H2) and cross-skill glob
pollution (B4-L1).

These are OS-level invariants: they verify the phase *definition* (the
DSL YAML frontmatter + instruction text), not LLM behaviour. No LLM call
is made; no mocks are needed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

PHASE_PATH = (
    Path(__file__).parent.parent
    / "src/reyn/stdlib/skills/skill_improver/phases/copy_to_work.md"
)


def _parse_frontmatter(text: str) -> dict:
    """Minimal frontmatter parser (mirrors compiler/parser.py logic)."""
    import yaml

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


def test_max_act_turns_sufficient(frontmatter):
    """Tier 2: copy_to_work max_act_turns must be >= 5 (B4-H2).

    The copy workflow requires 3 pipeline stages (glob → read → write).
    With budget=3 the LLM exhausted turns on redundant reads before
    writing, leaving the work dir uncreated. 5 is the safe minimum;
    the file currently sets 6.
    """
    turns = frontmatter.get("max_act_turns")
    assert turns is not None, "copy_to_work.md must declare max_act_turns"
    assert int(turns) >= 5, (
        f"copy_to_work max_act_turns must be >= 5 to survive redundant reads "
        f"(B4-H2); got {turns}"
    )


def test_glob_scoped_to_original_dsl_root(phase_text: str):
    """Tier 2: glob instructions must scope to original_dsl_root (B4-L1).

    The phase instructions must tell the LLM to glob using the
    target skill's root path, not a parent directory. A glob of a parent
    path (e.g. ``src/reyn/stdlib/skills/**/*.md``) matches all sibling
    skills and wastes act turns reading unrelated files.
    """
    # The instructions must reference the token that the LLM should
    # substitute — the glob prefix must start with original_dsl_root.
    assert "<original_dsl_root>/**/" in phase_text, (
        "copy_to_work.md glob patterns must use <original_dsl_root> as prefix "
        "to scope reads to the target skill only (B4-L1)"
    )


def test_glob_warns_against_parent_paths(phase_text: str):
    """Tier 2: phase instructions must explicitly warn against parent-path globs (B4-L1).

    Without an explicit prohibition the LLM widens the glob to the
    parent directory and pulls in 39+ unrelated skill files.
    """
    assert "parent" in phase_text.lower() or "sibling" in phase_text.lower(), (
        "copy_to_work.md must warn the LLM not to glob parent directories or "
        "sibling skills (B4-L1)"
    )
