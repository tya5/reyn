"""Tier 2: ``shape_only`` DSL annotation — render_shape_only_blocks invariants.

FP-0008 PR-M structural fix: the skill DSL gains a ``json shape_only``
code-block annotation that automatically converts string values in a
JSON example block to ``<KEY_FROM_ARTIFACT>`` uppercase placeholders and
injects a Critical warning before the block.

This file pins the behavioral contract of the annotation function:

  1. String values inside the JSON are replaced with
     ``<UPPER_CASE_KEY_FROM_ARTIFACT>`` placeholders.
  2. The ``shape_only`` marker is stripped from the rendered info string
     (the block appears as plain ``json`` to the LLM).
  3. A Critical warning paragraph is prepended before the block.
  4. Blocks WITHOUT the ``shape_only`` annotation pass through completely
     unchanged.
  5. The function is idempotent: applying it twice to the same text
     produces the same result as applying it once.
  6. The swe_bench skill's explore phase compiled instructions contain
     the annotation-derived placeholders (= integration: compiler applies
     render_shape_only_blocks to phase.instructions in expand_phase).

Design constraints (from testing policy):
  - Tier 2: OS invariant / subsystem contract.
  - No mocks, no private-state assertions, no format-pinning.
  - Each docstring starts with ``Tier 2:``.
"""
from __future__ import annotations

import textwrap


def _call(text: str) -> str:
    """Import and call render_shape_only_blocks."""
    from reyn.compiler.shape_renderer import render_shape_only_blocks
    return render_shape_only_blocks(text)


# ── Core transformation invariants ─────────────────────────────────────────

def test_shape_only_annotation_replaces_string_values_with_placeholders() -> None:
    """Tier 2: string values in a shape_only block become <KEY_FROM_ARTIFACT>."""
    src = textwrap.dedent("""\
        ```json shape_only
        {
          "instance_id": "django__django-12345",
          "repo": "django/django"
        }
        ```
    """)
    result = _call(src)
    assert "<INSTANCE_ID_FROM_ARTIFACT>" in result, (
        "render_shape_only_blocks must replace string values with "
        "<KEY_FROM_ARTIFACT> placeholders (PR-M DSL annotation invariant)"
    )
    assert "<REPO_FROM_ARTIFACT>" in result, (
        "render_shape_only_blocks must derive the placeholder from the "
        "field key name (e.g. repo → REPO)"
    )
    # Original realistic values must NOT appear in the output
    assert "django__django-12345" not in result, (
        "render_shape_only_blocks must remove the original realistic string value"
    )
    assert "django/django" not in result, (
        "render_shape_only_blocks must remove the original realistic repo value"
    )


def test_shape_only_annotation_strips_marker_from_rendered_prompt() -> None:
    """Tier 2: the rendered output does NOT contain the 'shape_only' marker.

    The ``shape_only`` marker is compiler-only; the LLM must never see it.
    The block should appear as a plain ``json`` fence after transformation.
    """
    src = textwrap.dedent("""\
        ```json shape_only
        {"key": "value"}
        ```
    """)
    result = _call(src)
    assert "shape_only" not in result, (
        "render_shape_only_blocks must strip 'shape_only' from the output "
        "— the LLM must not see the annotation marker"
    )
    assert "```json" in result, (
        "render_shape_only_blocks must keep the ```json fence opening "
        "(without the shape_only marker)"
    )


def test_shape_only_annotation_injects_critical_warning() -> None:
    """Tier 2: a Critical warning paragraph is prepended before the block."""
    src = textwrap.dedent("""\
        ```json shape_only
        {"key": "value"}
        ```
    """)
    result = _call(src)
    lower = result.lower()
    assert "critical" in lower, (
        "render_shape_only_blocks must inject a Critical warning before the "
        "transformed block"
    )
    # The warning must appear BEFORE the ```json fence
    critical_pos = lower.find("critical")
    fence_pos = result.find("```json")
    assert critical_pos < fence_pos, (
        "The Critical warning must appear before the ```json fence in the "
        "rendered output"
    )


def test_blocks_without_shape_only_annotation_pass_through_unchanged() -> None:
    """Tier 2: regular ``json`` blocks without shape_only are left unchanged."""
    original = textwrap.dedent("""\
        Some text before.

        ```json
        {
          "key": "a real value",
          "other": 42
        }
        ```

        Some text after.
    """)
    result = _call(original)
    assert result == original, (
        "render_shape_only_blocks must leave blocks without the shape_only "
        "annotation completely unchanged"
    )


def test_shape_only_annotation_is_idempotent() -> None:
    """Tier 2: applying render_shape_only_blocks twice gives the same result.

    The transformed output contains no ``shape_only`` markers, so a second
    pass must leave it unchanged (= no double-transformation).
    """
    src = textwrap.dedent("""\
        ```json shape_only
        {"instance_id": "django__django-12345"}
        ```
    """)
    once = _call(src)
    twice = _call(once)
    assert once == twice, (
        "render_shape_only_blocks must be idempotent: applying it twice "
        "must produce the same result as applying it once"
    )


def test_shape_only_annotation_preserves_non_string_values() -> None:
    """Tier 2: non-string JSON values (numbers, booleans, null) are NOT replaced."""
    src = textwrap.dedent("""\
        ```json shape_only
        {
          "name": "django__django-12345",
          "count": 42,
          "active": true,
          "score": 3.14,
          "note": null
        }
        ```
    """)
    result = _call(src)
    # String value replaced
    assert "<NAME_FROM_ARTIFACT>" in result, "string value must be replaced"
    # Non-string values preserved
    assert "42" in result, "integer must be preserved"
    assert "true" in result, "boolean must be preserved"
    assert "3.14" in result, "float must be preserved"
    assert "null" in result, "null must be preserved"


def test_shape_only_annotation_nested_objects_replace_leaf_strings() -> None:
    """Tier 2: string values in nested objects are replaced by leaf key name."""
    src = textwrap.dedent("""\
        ```json shape_only
        {
          "type": "swe_bench_input",
          "data": {
            "instance_id": "django__django-12345",
            "repo": "django/django"
          }
        }
        ```
    """)
    result = _call(src)
    # Top-level string value
    assert "<TYPE_FROM_ARTIFACT>" in result, (
        "top-level string value must be replaced by its key placeholder"
    )
    # Nested string values (uses leaf key, not dot-path)
    assert "<INSTANCE_ID_FROM_ARTIFACT>" in result
    assert "<REPO_FROM_ARTIFACT>" in result


# ── Integration: compiler applies annotation to phase instructions ──────────

def test_shape_only_annotation_applied_to_swe_bench_explore_phase() -> None:
    """Tier 2: explore phase compiled instructions reflect shape_only transformation.

    This is the end-to-end integration pin: the compiler (expand_phase in
    expander.py) must call render_shape_only_blocks on phase.instructions.
    The explore.md source uses ``json shape_only`` with natural-looking values;
    the compiled output must contain ``_FROM_ARTIFACT`` placeholders.
    """
    from pathlib import Path

    from reyn.compiler.loader import load_dsl_skill

    skill_root = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "stdlib" / "skills" / "swe_bench"
    )
    skill = load_dsl_skill(skill_root / "skill.md")
    explore_instructions = skill.phases["explore"].instructions

    assert "_FROM_ARTIFACT" in explore_instructions, (
        "Compiled explore.instructions must contain _FROM_ARTIFACT placeholders. "
        "The expander must apply render_shape_only_blocks to phase.instructions "
        "(PR-M DSL annotation integration invariant)."
    )
    assert "shape_only" not in explore_instructions, (
        "Compiled explore.instructions must NOT contain the 'shape_only' marker "
        "(= the annotation must be stripped at compile time, not passed to the LLM)."
    )
