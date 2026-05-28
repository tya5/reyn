"""Tier 2: FP-0008 PR-J -- explore.md placeholder shape (PR-F class N+1).

Defect surfaced by sandbox_2 v6 calibration retry (2026-05-28): 2 of 9
aborts hit `file_not_found` cascades because the LLM literal-copied
the example values from explore.md's input-artifact JSON shape block:

  Pre-PR-J explore.md:
    "instance_id": "django__django-12345",
    "repo": "django/django",

A weak LLM (gemini-2.5-flash-lite) treated `"django/django"` as the
literal repo name (= not pattern-match value, but actual data) and
fabricated context anchored on that. File reads + grep ops then hit
file_not_found because the actual task was an astropy instance, not
a django one.

This is the same class of bug as PR-F Defect 1 (setup.md `<base_commit>`
literal-emission). PR-G partial fix gap: PR-G rewrote explore.md's
field-access section but used realistic-looking example values
(`"django__django-12345"`, `"django/django"`) instead of ALL-CAPS
obvious-placeholder shape that PR-F established as the canonical
mitigation.

This file pins:
  1. explore.md's JSON example uses `<*_FROM_ARTIFACT>` ALL-CAPS
     placeholder shape (NOT `django/django` or other realistic-
     looking literals that weak LLMs might copy).
  2. explore.md includes an explicit Critical warning that the
     placeholders are SHAPE documentation, not values to copy.
  3. No literal-looking realistic example values (django/django,
     django__django-N) appear inside the input-artifact JSON shape
     block.

Tier rule discipline: every test docstring opens with Tier 2; no
mocks; no private-state assertions; no format-pinning.
"""
from __future__ import annotations

from pathlib import Path

_SKILL_ROOT = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)


def _read_explore_md() -> str:
    return (_SKILL_ROOT / "phases" / "explore.md").read_text(encoding="utf-8")


def test_explore_md_uses_all_caps_placeholder_in_artifact_block() -> None:
    """Tier 2: explore.md input-artifact JSON shape uses _FROM_ARTIFACT placeholders."""
    text = _read_explore_md()
    # At least one ALL-CAPS _FROM_ARTIFACT marker must appear in the
    # input-artifact block (= the canonical PR-F-style mitigation shape).
    assert "_FROM_ARTIFACT" in text, (
        "explore.md must use ALL-CAPS `<*_FROM_ARTIFACT>` placeholder "
        "shape in the input-artifact JSON example (PR-F class extension)"
    )


def test_explore_md_warns_against_literal_copy() -> None:
    """Tier 2: explore.md includes an explicit Critical warning against literal copy.

    The PR-F established pattern: the LLM must understand the
    angle-bracketed placeholders are SHAPE documentation. Without an
    explicit warning, weak LLMs literal-copy.
    """
    text = _read_explore_md().lower()
    # Soft check: "critical" + "not a literal" / "not literal" / "shape"
    # phrasing must appear.
    assert "critical" in text, (
        "explore.md must include a Critical warning section about "
        "placeholder vs value distinction"
    )
    assert (
        "not" in text and ("literal" in text or "copy" in text or "shape" in text)
    ), (
        "explore.md's Critical section must articulate that placeholders "
        "are NOT literal values to copy"
    )


def test_explore_md_does_not_use_realistic_placeholder_repo_name() -> None:
    """Tier 2: explore.md does NOT show `django/django` as an example repo.

    Realistic-looking example values triggered literal-copy by weak
    LLMs in v6 retry. The PR-J fix replaces them with ALL-CAPS shape
    placeholders. This test catches accidental regression to
    realistic-example shape (= e.g. future author rewrites with
    'requests/requests' or 'numpy/numpy').

    Rule (negative): no `<owner>/<owner>` pattern with realistic
    org/repo names inside the input-artifact JSON shape block.
    Note: this DOES allow such names in non-shape contexts (e.g. the
    intro paragraph mentioning "SWE-bench Verified covers Django,
    Astropy, ..." would be fine).
    """
    text = _read_explore_md()
    # Find the input-artifact JSON shape block by anchor + closing brace.
    anchor = "Where to find the input fields"
    if anchor in text:
        block_start = text.index(anchor)
        # Search up to the next h2 / h1 boundary
        block_end_candidates = [
            text.find("\n## ", block_start + 1),
            text.find("\n# ", block_start + 1),
            len(text),
        ]
        block_end = min([c for c in block_end_candidates if c > 0])
        block = text[block_start:block_end]
    else:
        block = text
    forbidden_realistic = (
        '"django/django"',
        '"django__django-12345"',
        '"requests/requests"',
        '"numpy/numpy"',
    )
    for phrase in forbidden_realistic:
        assert phrase not in block, (
            f"explore.md input-artifact shape block re-introduces a "
            f"realistic-looking placeholder {phrase!r}. Weak LLMs "
            f"literal-copy these. Use ALL-CAPS `<*_FROM_ARTIFACT>` "
            f"shape instead (PR-F established pattern)."
        )


def test_explore_md_artifact_block_uses_six_placeholder_fields() -> None:
    """Tier 2: ALL-CAPS placeholders cover the six swe_bench_input fields.

    The placeholder shape preserves the field names + types so the LLM
    can still pattern-match the artifact shape; only the example
    values change from realistic to obvious-placeholder.
    """
    text = _read_explore_md()
    # Each of the six fields must appear in the input-artifact block.
    for field in ("instance_id", "repo", "base_commit",
                  "problem_statement", "hints_text", "test_patch"):
        assert field in text, (
            f"explore.md must still reference the {field!r} field "
            f"(= field-access pattern preserved post-PR-J)"
        )
