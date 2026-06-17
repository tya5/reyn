"""Tier 2: ``_resolve_skill_ref`` recovers LLM-hallucinated path forms.

Pinned invariants:

- Form 1 (bare name): unchanged contract, still resolves via
  ``resolve_skill_path``.
- Form 2 (``<name>/skill.md``): unchanged contract, still resolves
  via leading-segment lookup.
- **Form 4 (B47-NF-eval-1)**: router LLMs at flash-lite hallucinate
  paths like ``"skills/direct_llm.yaml"``, ``"skills/direct_llm.py"``,
  ``"skills/skill__direct_llm.py"`` when constructing
  ``target_skill_path`` for the eval skill. The resolver now extracts
  a bare-skill-name candidate from the last path segment after
  stripping ``skill__`` prefix and the ``.yaml`` / ``.py`` / ``.md``
  extension, tries it against ``resolve_skill_path``, and on success
  returns the resolved skill.
- Form 3 fall-through preserved: when the hallucination-recovery
  candidate also fails, the literal-path interpretation kicks in for
  pre-form-4 callers who genuinely pass a relative path.
- Reproducibility evidence (2026-05-21 N=3 reproduction of B47-W2-S5
  eval scenario): every run hit FileNotFoundError with a different
  hallucinated path. After form 4, all 3 resolve to ``direct_llm``.

testing.ja.md compliance:
- No mocks. Real ``_resolve_skill_ref`` called against the real
  stdlib skill tree.
- Tier 2 contract pin: the resolver's documented input → output map.
- No private-state assertions.
"""
from __future__ import annotations

import pytest

from reyn.core.op_runtime.run_skill import _resolve_skill_ref
from reyn.skill.skill_paths import SkillNotFoundError

# ---------------------------------------------------------------------------
# Form 4: LLM-hallucination recovery — the headline B47-NF-eval-1 fix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hallucinated_ref", [
    "skills/direct_llm.yaml",            # n3 of B47 reproduction
    "skills/direct_llm.py",              # n2
    "skills/skill__direct_llm.py",       # n1
])
def test_form4_recovers_known_hallucinations_to_direct_llm(hallucinated_ref):
    """Tier 2: each of the 3 N=3-reproduced hallucination
    forms resolves to direct_llm via form-4 normalization."""
    md_path, _path_for_hash, _root = _resolve_skill_ref(hallucinated_ref)
    assert md_path.endswith("/stdlib/skills/direct_llm/skill.md"), (
        f"hallucinated ref {hallucinated_ref!r} should resolve to "
        f"direct_llm via form-4 recovery. Got: {md_path!r}"
    )


@pytest.mark.parametrize("ref,expected_skill", [
    # Strip .yaml extension + nested path
    ("skills/word_stats_demo.yaml", "word_stats_demo"),
    # Strip .py extension + skill__ prefix + nested path
    ("skills/skill__word_stats_demo.py", "word_stats_demo"),
    # Strip just .md extension + skill__ prefix
    ("skill__direct_llm.md", "direct_llm"),
    # Single-segment with extension
    ("direct_llm.yaml", "direct_llm"),
])
def test_form4_strips_extension_and_skill_prefix(ref, expected_skill):
    """Tier 2: form-4 covers the normalization combinations — extension
    stripping, ``skill__`` prefix stripping, and nested path tail
    extraction, in either order or combination."""
    md_path, _, _ = _resolve_skill_ref(ref)
    assert md_path.endswith(f"/stdlib/skills/{expected_skill}/skill.md"), (
        f"ref {ref!r} should normalize to {expected_skill!r}. Got: {md_path!r}"
    )


# ---------------------------------------------------------------------------
# Form 1-3: regression guards
# ---------------------------------------------------------------------------


def test_form1_bare_name_unchanged():
    """Tier 2: regression — bare skill name still resolves identically
    to pre-form-4 behavior."""
    md_path, _, _ = _resolve_skill_ref("direct_llm")
    assert md_path.endswith("/stdlib/skills/direct_llm/skill.md")


def test_form2_short_name_slash_skill_md_unchanged():
    """Tier 2: regression — ``<name>/skill.md`` form still resolves
    via leading-segment lookup."""
    md_path, _, _ = _resolve_skill_ref("direct_llm/skill.md")
    assert md_path.endswith("/stdlib/skills/direct_llm/skill.md")


def test_form3_genuine_literal_path_passes_through():
    """Tier 2: regression — a multi-segment literal path that doesn't
    match any stdlib skill (= form 4 candidate also fails) falls
    through to literal-path interpretation. The returned md_path is
    the input verbatim — caller's open() will fail with FileNotFound
    as before, NOT silently resolve to something else."""
    ref = "reyn/local/my_imaginary_app/skill.md"
    md_path, _path_for_hash, root = _resolve_skill_ref(ref)
    assert md_path == ref, (
        f"genuine literal path must pass through verbatim. Got: {md_path!r}"
    )
    assert root is None


def test_form4_does_not_shadow_form1_for_valid_bare_name():
    """Tier 2: form-4 only fires after forms 1-3 fail. A genuine bare
    name with a real skill on disk must not trigger form-4
    normalization. We verify by checking that the input was treated
    as form 1 — the result path matches form 1's output."""
    md_path, _, _ = _resolve_skill_ref("word_stats_demo")
    assert md_path.endswith("/stdlib/skills/word_stats_demo/skill.md")


# ---------------------------------------------------------------------------
# Form 4: failure modes — when normalization can't help
# ---------------------------------------------------------------------------


def test_form4_unknown_skill_falls_through_to_form3():
    """Tier 2: when the form-4 candidate can't be resolved either (=
    truly unknown skill name), the function falls through to form 3
    literal-path interpretation rather than raising."""
    ref = "skills/totally_made_up_skill_x9z7.yaml"
    md_path, _, root = _resolve_skill_ref(ref)
    # Form 3: returned as-is, no resolution. Caller's open() will
    # FileNotFoundError later — that surfaces the issue to the user.
    assert md_path == ref
    assert root is None


def test_form1_strictness_preserved_for_truly_bare_unknown_name():
    """Tier 2: a bare name with no matching skill still raises
    SkillNotFoundError — form 4 does not silently allow unknown bare
    names through (= bare names go to form 1, which raises)."""
    with pytest.raises(SkillNotFoundError):
        _resolve_skill_ref("totally_made_up_skill_x9z7_bare")
