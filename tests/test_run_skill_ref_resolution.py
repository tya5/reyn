"""Tier 2: run_skill op `op.skill` reference resolution (B41-NF-S7-1 fix).

Pinned invariants:

- Bare skill name resolves via ``resolve_skill_path`` search order
  (reyn/local → reyn/project → stdlib/skills). [pre-existing behavior]
- Short ``<name>/skill.md`` reference resolves the leading segment via the
  same search order. [B41 fix — eval router LLMs construct this shape when
  describing a stdlib skill via the ``target_skill_path`` field name; the
  pre-fix runtime treated it as a CWD-relative literal and failed.]
- Multi-segment literal paths (e.g. ``reyn/local/my_app/skill.md``) pass
  through unchanged. [pre-existing behavior]
- Unknown leading segment in ``<name>/skill.md`` falls through to literal
  path interpretation (= preserves any pre-B41 caller that genuinely passes
  a 2-segment relative path that happens to look like the new shape).

Reference: B41-NF-S7-1 retrospective + W2-S7 patch-isolation evidence.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.skill.run_skill import _resolve_skill_ref
from reyn.skill.skill_paths import SkillNotFoundError, stdlib_root

# ---------------------------------------------------------------------------
# Form 1: bare skill name (pre-existing behavior, regression guard)
# ---------------------------------------------------------------------------


def test_bare_skill_name_resolves_via_stdlib():
    """Tier 2: bare name like 'direct_llm' resolves to stdlib skill dir."""
    skill_md_path, path_for_hash, skill_root = _resolve_skill_ref("direct_llm")

    sl = stdlib_root()
    expected_dir = sl / "skills" / "direct_llm"
    assert skill_md_path == str(expected_dir / "skill.md")
    assert path_for_hash == expected_dir / "skill.md"
    assert skill_root == str(sl)


def test_unknown_bare_skill_name_raises():
    """Tier 2: bare name that doesn't exist anywhere raises SkillNotFoundError."""
    with pytest.raises(SkillNotFoundError):
        _resolve_skill_ref("definitely_does_not_exist_xyz")


# ---------------------------------------------------------------------------
# Form 2: short <name>/skill.md (B41 fix)
# ---------------------------------------------------------------------------


def test_short_name_with_skill_md_resolves_via_stdlib():
    """Tier 2: '<stdlib_name>/skill.md' resolves leading segment (B41-NF-S7-1).

    The eval router LLM constructs paths like 'direct_llm/skill.md' when
    the target_skill_path field name implies a path is expected. Pre-B41,
    this triggered a CWD-relative FileNotFoundError. Post-fix, the leading
    segment is resolved via stdlib paths.
    """
    skill_md_path, path_for_hash, skill_root = _resolve_skill_ref("direct_llm/skill.md")

    sl = stdlib_root()
    expected_dir = sl / "skills" / "direct_llm"
    assert skill_md_path == str(expected_dir / "skill.md")
    assert path_for_hash == expected_dir / "skill.md"
    assert skill_root == str(sl)


def test_short_name_with_skill_md_falls_through_when_unknown(tmp_path: Path, monkeypatch):
    """Tier 2: '<unknown_name>/skill.md' falls through to literal path interpretation.

    Preserves any pre-B41 caller that genuinely passed a 2-segment relative
    path whose leading segment happens not to resolve via the stdlib search
    order. The caller's literal-path interpretation is then attempted by
    ``load_dsl_skill`` (not exercised by this unit) and will surface a
    FileNotFoundError if the literal path also doesn't exist.
    """
    monkeypatch.chdir(tmp_path)

    skill_md_path, path_for_hash, skill_root = _resolve_skill_ref("not_a_skill/skill.md")

    assert skill_md_path == "not_a_skill/skill.md"
    assert path_for_hash == Path("not_a_skill/skill.md")
    assert skill_root is None


# ---------------------------------------------------------------------------
# Form 3: multi-segment literal path (pre-existing behavior, regression guard)
# ---------------------------------------------------------------------------


def test_multi_segment_literal_path_passes_through():
    """Tier 2: 'reyn/local/my_app/skill.md' passes through unchanged.

    Pre-existing behavior for explicit paths that traverse the reyn tree.
    The literal path is passed to load_dsl_skill which validates existence
    at load time.
    """
    ref = "reyn/local/my_app/skill.md"
    skill_md_path, path_for_hash, skill_root = _resolve_skill_ref(ref)

    assert skill_md_path == ref
    assert path_for_hash == Path(ref)
    assert skill_root is None


def test_three_segment_path_treated_as_literal():
    """Tier 2: paths with 3+ segments are NOT subject to form-2 fallback.

    Only ``<name>/skill.md`` (exactly 2 segments) is recognized as the
    LLM-friendly short form. Longer paths are treated as explicit literals
    so resolution behavior remains deterministic for callers that pass
    workspace-relative paths.
    """
    ref = "some/nested/dir/skill.md"
    skill_md_path, path_for_hash, skill_root = _resolve_skill_ref(ref)

    assert skill_md_path == ref
    assert path_for_hash == Path(ref)
    assert skill_root is None


def test_path_ending_skill_md_but_not_two_segments():
    """Tier 2: 'skill.md' alone (no slash) is treated as a literal path.

    Although ``skill_ref.endswith('.md')`` is True, the bare ``skill.md``
    string has no leading skill-name segment, so it cannot resolve via
    stdlib. The literal-path branch handles it (and ``load_dsl_skill``
    will fail with FileNotFoundError if no such file exists in CWD).
    """
    ref = "skill.md"
    skill_md_path, path_for_hash, skill_root = _resolve_skill_ref(ref)

    assert skill_md_path == ref
    assert skill_root is None
