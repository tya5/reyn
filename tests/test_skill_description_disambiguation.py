"""Tier 2 OS invariant: skill__eval vs skill__skill_improver description divergence.

Root cause (B28-MED-2): the LLM invoked skill__skill_improver for S7
(eval_run_direct_llm) because both skill descriptions shared evaluation-flavoured
keywords ("evaluate", "score", "iterate") with no decisive distinguishing phrase.

Fix: descriptions now lead with decisive verbs ("Run" vs "Iterate") and state
distinct input/output shapes. This test pins the keyword-overlap ceiling so
future edits cannot accidentally re-blend the two descriptions.

Invariant tested (Tier 2 — OS-level routing prerequisite):
  - Both skill.md files have a non-empty description.
  - Each description starts with its decisive leading verb (Run / Iterate).
  - The token-level word overlap between the two descriptions is <= K_MAX_OVERLAP.
    K is calibrated to the post-fix state (9 shared tokens, all articles or
    unavoidable domain terms: a, the, does, not, eval, run, score, skill, output).
    K=10 gives a 1-token margin while catching any future re-blending.

Testing policy (docs/deep-dives/contributing/testing.ja.md):
  - Tier 2: OS invariant — verifies the routing-prerequisite DSL contract.
  - No mocks — reads skill.md via _split_frontmatter (same path as
    enumerate_available_skills / session.py).
  - No private-state assertions.
"""
from __future__ import annotations

import re
from pathlib import Path

from reyn.core.compiler.parser import _split_frontmatter
from reyn.skill.skill_paths import resolve_skill_path

# ── constants ──────────────────────────────────────────────────────────────────

# Calibrated against the post-fix overlap set:
#   {'a', 'does', 'eval', 'not', 'output', 'run', 'score', 'skill', 'the'}
# = 9 tokens, all articles or domain terms that cannot be removed without
# losing meaning. K=10 gives exactly 1 token of slack.
K_MAX_OVERLAP: int = 10

# Decisive leading verbs that distinguish the two skills at first glance.
_EVAL_LEADING_VERB = "Run"
_IMPROVER_LEADING_VERB = "Iterate"


# ── helpers ────────────────────────────────────────────────────────────────────


def _load_description(skill_name: str) -> str:
    """Return the first-line description from a stdlib skill's frontmatter.

    Uses _split_frontmatter — the same code path as enumerate_available_skills
    in session.py — so this test stays in sync with the actual runtime behavior.
    Returns the first line only (matching session.py line 460 behaviour).
    """
    skill_dir, _ = resolve_skill_path(skill_name)
    skill_md = Path(skill_dir) / "skill.md"
    fm, _ = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
    raw = fm.get("description") or ""
    return str(raw).strip().splitlines()[0]


def _token_set(description: str) -> set[str]:
    """Return the lowercased word tokens in description (punctuation stripped)."""
    return set(re.sub(r"[^a-z0-9 ]", " ", description.lower()).split())


# ── tests ──────────────────────────────────────────────────────────────────────


def test_eval_description_nonempty():
    """Tier 2: skill__eval has a non-empty description field."""
    desc = _load_description("eval")
    assert desc, "eval skill.md description must be non-empty"


def test_skill_improver_description_nonempty():
    """Tier 2: skill__skill_improver has a non-empty description field."""
    desc = _load_description("skill_improver")
    assert desc, "skill_improver skill.md description must be non-empty"


def test_eval_description_leading_verb():
    """Tier 2: skill__eval description starts with decisive leading verb 'Run'.

    B28-MED-2 fix: the leading verb disambiguates eval (single-pass run) from
    skill_improver (iterative loop). 'Run' must be the first word so it appears
    in the 80-char list_skills truncation window seen by the router LLM.
    """
    desc = _load_description("eval")
    assert desc.startswith(_EVAL_LEADING_VERB), (
        f"eval description must start with {_EVAL_LEADING_VERB!r} (B28-MED-2 fix); "
        f"got: {desc!r}"
    )


def test_skill_improver_description_leading_verb():
    """Tier 2: skill__skill_improver description starts with decisive verb 'Iterate'.

    B28-MED-2 fix: 'Iterate' signals the repeated-loop nature of skill_improver,
    contrasting with eval's single-pass 'Run'. Must appear in the first 80 chars.
    """
    desc = _load_description("skill_improver")
    assert desc.startswith(_IMPROVER_LEADING_VERB), (
        f"skill_improver description must start with {_IMPROVER_LEADING_VERB!r} "
        f"(B28-MED-2 fix); got: {desc!r}"
    )


def test_description_token_overlap_within_budget():
    """Tier 2: word-token overlap between eval and skill_improver descriptions <= K_MAX_OVERLAP.

    B28-MED-2 fix: the two descriptions previously shared evaluation-flavoured
    keywords that caused LLM mis-routing. After the fix the overlap consists only
    of unavoidable articles and domain terms (a, the, does, not, eval, run, score,
    skill, output = 9 tokens). K=10 catches any future re-blending that reintroduces
    shared evaluation adjectives without false-positiving on the irreducible minimum.

    If this test fails, a recent description edit has re-blended the two. Inspect the
    overlap set printed below and remove shared evaluation adjectives from one or both
    descriptions so that each leads with a clearly distinct verb and concept.
    """
    eval_desc = _load_description("eval")
    improver_desc = _load_description("skill_improver")

    eval_tokens = _token_set(eval_desc)
    improver_tokens = _token_set(improver_desc)
    overlap = eval_tokens & improver_tokens

    assert len(overlap) <= K_MAX_OVERLAP, (
        f"Word-token overlap between eval and skill_improver descriptions is "
        f"{len(overlap)} > K_MAX_OVERLAP={K_MAX_OVERLAP}. "
        f"Overlap set: {sorted(overlap)}. "
        f"Re-blending detected — tighten one or both descriptions so the leading "
        f"verbs (Run vs Iterate) and I/O shapes remain clearly distinct."
    )
