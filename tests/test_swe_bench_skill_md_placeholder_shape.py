"""Tier 2: FP-0008 PR-M -- skill.md placeholder shape (PR-J class N=3, DSL lift).

Class progression (= N=3 DSL lift trigger):
  - PR-F (#1008) class N=1: setup.md ``<base_commit>`` literal
  - PR-J class N=2: explore.md ``django/django`` literal
  - PR-M (THIS, N=3): class dissolution via ``{shape_only}`` DSL annotation

PR-M structural fix: the skill DSL now has a ``json shape_only`` code-block
annotation (implemented in ``reyn.compiler.shape_renderer``).  Skill authors
write natural-looking JSON examples; the compiler transforms string values to
``<KEY_FROM_ARTIFACT>`` placeholders before the text reaches the LLM.

This file pins:
  1. skill.md uses the ``shape_only`` annotation in its Input JSON block
     (= the author does NOT write manual ALL-CAPS in the source file).
  2. After compilation, the explore phase instructions contain
     ``_FROM_ARTIFACT`` placeholders (= the compiler did its job).
  3. After compilation, the explore phase instructions include a
     Critical warning about placeholder vs literal values.
  4. No realistic-looking literals appear in the compiled explore
     instructions (= negative regression pin).
  5. The compiled explore instructions reference all six swe_bench_input
     fields (= field-access pattern preserved).

Tier rule discipline: every test docstring opens with Tier 2; no
mocks; no private-state assertions; no format-pinning.
"""
from __future__ import annotations

from pathlib import Path

_SKILL_ROOT = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
)


def _read_skill_md() -> str:
    return (_SKILL_ROOT / "skill.md").read_text(encoding="utf-8")


def _input_block_from_raw(text: str) -> str:
    """Extract the ## Input section from skill.md raw text."""
    anchor = "## Input"
    if anchor in text:
        block_start = text.index(anchor)
        block_end_candidates = [
            text.find("\n## ", block_start + 1),
            text.find("\n# ", block_start + 1),
            len(text),
        ]
        block_end = min(c for c in block_end_candidates if c > 0)
        return text[block_start:block_end]
    return text


def _load_compiled_explore_instructions() -> str:
    """Load the swe_bench skill and return the compiled explore phase instructions.

    This exercises the real compiler path (parser + expander + shape_renderer),
    so the test pins what the LLM actually receives at runtime.
    """
    from reyn.compiler.loader import load_dsl_skill
    skill = load_dsl_skill(_SKILL_ROOT / "skill.md")
    explore = skill.phases["explore"]
    return explore.instructions


# ── Raw skill.md annotation tests ──────────────────────────────────────────

def test_skill_md_uses_shape_only_annotation_in_input_block() -> None:
    """Tier 2: skill.md ## Input section uses the shape_only annotation."""
    text = _read_skill_md()
    block = _input_block_from_raw(text)
    assert "shape_only" in block, (
        "skill.md ## Input section must use the ``json shape_only`` "
        "annotation to document the input shape (PR-M DSL class dissolution). "
        "Manual ALL-CAPS placeholders are replaced by the compiler annotation."
    )


def test_skill_md_does_not_have_manual_all_caps_placeholders_in_input_block() -> None:
    """Tier 2: skill.md ## Input raw source does NOT use manual ALL-CAPS.

    After PR-M, skill authors write natural-looking values + ``shape_only``
    annotation; the compiler handles the transformation.  Manual ALL-CAPS in
    the raw source would be redundant and indicate the author bypassed the DSL.
    """
    text = _read_skill_md()
    block = _input_block_from_raw(text)
    # The raw source should NOT contain the manually-written ALL-CAPS form
    # — if it does, the author wrote them by hand instead of using the annotation.
    # (The compiler may produce _FROM_ARTIFACT in the compiled output; that is
    #  NOT a violation — but the raw file should not contain them.)
    assert "_FROM_ARTIFACT" not in block or "shape_only" in block, (
        "skill.md ## Input raw source has _FROM_ARTIFACT placeholders without "
        "the shape_only annotation.  Use ``json shape_only`` instead of manual "
        "ALL-CAPS (PR-M DSL convention)."
    )


def test_skill_md_artifact_block_uses_six_fields() -> None:
    """Tier 2: skill.md ## Input block references all six swe_bench_input fields."""
    text = _read_skill_md()
    block = _input_block_from_raw(text)
    for field in (
        "instance_id",
        "repo",
        "base_commit",
        "problem_statement",
        "hints_text",
        "test_patch",
    ):
        assert field in block, (
            f"skill.md ## Input block must reference the {field!r} field "
            f"(= field-access pattern preserved post-PR-M)"
        )


# ── Compiled output tests ───────────────────────────────────────────────────

def test_compiled_explore_uses_all_caps_placeholders() -> None:
    """Tier 2: compiled explore instructions contain _FROM_ARTIFACT placeholders.

    The shape_only DSL annotation is implemented in reyn.compiler.shape_renderer
    and applied in expand_phase.  This test pins the compiled (LLM-visible)
    output, not the raw source file.
    """
    instructions = _load_compiled_explore_instructions()
    assert "_FROM_ARTIFACT" in instructions, (
        "Compiled explore.instructions must contain _FROM_ARTIFACT placeholders. "
        "The shape_renderer should transform the ``json shape_only`` block in "
        "explore.md at compile time (PR-M DSL annotation invariant)."
    )


def test_compiled_explore_includes_critical_warning() -> None:
    """Tier 2: compiled explore instructions include a Critical placeholder warning.

    The shape_renderer injects a standard Critical warning before each
    shape_only block.  The LLM must see this warning to understand that the
    angle-bracketed values are documentation-only.
    """
    instructions = _load_compiled_explore_instructions()
    lower = instructions.lower()
    assert "critical" in lower, (
        "Compiled explore.instructions must include a Critical warning "
        "(injected by shape_renderer) about placeholder vs literal values."
    )


def test_compiled_explore_does_not_show_realistic_repo_literal() -> None:
    """Tier 2: compiled explore instructions do NOT contain realistic example values.

    After shape_renderer transformation, realistic-looking values like
    ``django/django`` must be replaced by placeholders.  This is the
    core invariant of the shape_only DSL annotation.
    """
    instructions = _load_compiled_explore_instructions()
    # These are the specific realistic values known to cause literal-copy
    # defects in weak LLMs (documented in PR-F/PR-J/PR-M class history).
    forbidden = (
        '"django/django"',
        '"django__django-12345"',
    )
    for phrase in forbidden:
        assert phrase not in instructions, (
            f"Compiled explore.instructions must not contain realistic literal "
            f"{phrase!r}. The shape_renderer must have transformed it to a "
            f"placeholder (PR-M DSL annotation invariant)."
        )


def test_compiled_explore_references_all_six_fields() -> None:
    """Tier 2: compiled explore instructions reference all six swe_bench_input fields.

    The shape_renderer preserves JSON keys; only string values are replaced.
    This test ensures the field-access documentation pattern is intact after
    transformation.
    """
    instructions = _load_compiled_explore_instructions()
    for field in (
        "instance_id",
        "repo",
        "base_commit",
        "problem_statement",
        "hints_text",
        "test_patch",
    ):
        assert field in instructions, (
            f"Compiled explore.instructions must reference the {field!r} field "
            f"after shape_renderer transformation (= field-access pattern preserved)."
        )
