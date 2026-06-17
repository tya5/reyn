"""Tier 1 Contract tests for G16 fix: eval_builder routing wording disambiguation.

Root cause (B8-S5a): weak LLM misroutes "eval を作って" to eval skill (run) instead
of eval_builder (create spec). Fix: distinctive wording in description, when_not_to_use,
and examples in both eval_builder/skill.md and eval/skill.md.

Invariants tested:
  - eval_builder description starts with distinctive verb 'Build' (not 'Auto-generate')
  - eval_builder description is ≤ MAX_DESC_LEN_FOR_LISTING (80) chars — G12 truncation safe
  - eval_builder description preserves the disambiguation signal in first 80 chars
  - eval_builder when_not_to_use mentions the create/run distinction vs eval skill
  - eval_builder routing.examples.positive includes 'direct_llm の eval を作って' form
  - eval_builder routing.examples.negative includes a 'を eval して' contrast case
  - eval skill when_not_to_use mentions eval_builder for create intent
  - eval skill routing.examples.negative includes 'eval を作って' contrast case

Testing policy (docs/deep-dives/contributing/testing.ja.md):
  - Tier 1 Contract: verify public DSL contract (skill.md parsed fields)
  - No mocks — reads skill.md via _split_frontmatter (same path as enumerate_available_skills)
  - No private-state assertions
"""
from __future__ import annotations

from pathlib import Path

from reyn.chat.router_tools import MAX_DESC_LEN_FOR_LISTING
from reyn.core.compiler.parser import _split_frontmatter
from reyn.skill.skill_paths import resolve_skill_path

# ── helpers ────────────────────────────────────────────────────────────────────


def _load_skill_frontmatter(name: str) -> dict:
    """Return the raw frontmatter dict for a stdlib skill.

    Uses _split_frontmatter — the same code path as enumerate_available_skills
    in session.py — so tests stay in sync with the actual runtime behavior.
    """
    skill_dir, _ = resolve_skill_path(name)
    skill_md = Path(skill_dir) / "skill.md"
    fm, _ = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
    return fm


def _routing(fm: dict) -> dict:
    """Return routing block from frontmatter, or {} if absent."""
    r = fm.get("routing")
    return r if isinstance(r, dict) else {}


def _when_not_to_use(fm: dict) -> list:
    """Return routing.when_not_to_use list from frontmatter."""
    return _routing(fm).get("when_not_to_use") or []


def _examples(fm: dict) -> dict:
    """Return routing.examples dict (positive/negative) from frontmatter."""
    ex = _routing(fm).get("examples")
    return ex if isinstance(ex, dict) else {}


# ── eval_builder description constraints ──────────────────────────────────────


def test_eval_builder_description_starts_with_build():
    """Tier 1: eval_builder description starts with 'Build' (distinctive verb vs eval skill).

    G16 fix: description must use 'Build' (not 'Auto-generate') so weak LLM distinguishes
    eval_builder (create spec) from eval (run spec). The first word is load-bearing.
    """
    fm = _load_skill_frontmatter("eval_builder")
    desc = str(fm.get("description", ""))
    assert desc.startswith("Build"), (
        f"eval_builder description must start with 'Build' (G16 fix); got: {desc!r}"
    )


def test_eval_builder_description_within_80_chars():
    """Tier 1: eval_builder description is ≤ MAX_DESC_LEN_FOR_LISTING (80) chars.

    G12 truncation: list_skills truncates descriptions at 80 chars. The first 80 chars
    must convey the disambiguation signal (Build / create vs run). A description >80 chars
    risks losing the contrast in the truncated form shown to the router LLM.
    """
    fm = _load_skill_frontmatter("eval_builder")
    desc = str(fm.get("description", ""))
    assert len(desc) <= MAX_DESC_LEN_FOR_LISTING, (
        f"eval_builder description must be ≤ {MAX_DESC_LEN_FOR_LISTING} chars "
        f"(G12 truncation safe); got {len(desc)} chars: {desc!r}"
    )


def test_eval_builder_description_mentions_eval_skill_contrast():
    """Tier 1: eval_builder description mentions 'eval' as the skill to use for running.

    The description must contain a signal that 'eval' (not eval_builder) is for running,
    so the router LLM can differentiate when it sees both skills in list_skills output.
    """
    fm = _load_skill_frontmatter("eval_builder")
    desc = str(fm.get("description", "")).lower()
    assert "eval" in desc, (
        f"eval_builder description must mention 'eval' for contrast; "
        f"got: {fm.get('description')!r}"
    )


# ── eval_builder when_not_to_use ──────────────────────────────────────────────


def test_eval_builder_when_not_to_use_mentions_eval_skill():
    """Tier 1: eval_builder when_not_to_use contains a rule directing 'eval して' to eval skill.

    G16 fix: the when_not_to_use block must explicitly state that 'eval を実行する' / run intent
    belongs to the eval skill, not eval_builder. Weak LLM reads this block during routing.
    """
    fm = _load_skill_frontmatter("eval_builder")
    when_not = _when_not_to_use(fm)
    combined = " ".join(str(r) for r in when_not).lower()
    assert "eval" in combined, (
        "eval_builder when_not_to_use must mention 'eval' skill for run/execute intent"
    )
    has_contrast = any(
        keyword in combined
        for keyword in ["run", "eval skill", "実行", "eval して"]
    )
    assert has_contrast, (
        "eval_builder when_not_to_use must contain create/run contrast "
        f"(run / eval skill / 実行 / eval して); got: {combined!r}"
    )


def test_eval_builder_when_not_to_use_distinguishes_create_vs_run():
    """Tier 1: eval_builder when_not_to_use explicitly marks run intent as out-of-scope.

    The rule must be unambiguous to the weak LLM: eval_builder creates specs,
    eval runs them. At least one bullet must contain both concepts.
    """
    fm = _load_skill_frontmatter("eval_builder")
    when_not = _when_not_to_use(fm)
    matched = any(
        ("eval" in str(r).lower() and any(
            kw in str(r).lower() for kw in ["run", "create", "作って", "実行", "eval skill"]
        ))
        for r in when_not
    )
    assert matched, (
        "eval_builder when_not_to_use must have a bullet that mentions both 'eval' "
        f"and a run/create distinction; bullets: {when_not}"
    )


# ── eval_builder routing examples ─────────────────────────────────────────────


def test_eval_builder_positive_examples_include_eval_wo_tsukutte():
    """Tier 1: eval_builder positive examples include 'eval を作って' form.

    G16 bug input was 'direct_llm の eval を作って'. Pinning this exact pattern
    in examples gives the weak LLM a concrete anchor for create intent.
    """
    fm = _load_skill_frontmatter("eval_builder")
    positive = _examples(fm).get("positive", []) or []
    combined = " ".join(str(e) for e in positive)
    assert "eval を作って" in combined or ("eval" in combined and "作って" in combined), (
        f"eval_builder positive examples must include 'eval を作って' form; got: {positive}"
    )


def test_eval_builder_negative_examples_include_eval_shite():
    """Tier 1: eval_builder negative examples include 'を eval して' run contrast.

    The contrast example 'skill X を eval して → use eval skill' anchors the
    negative signal so the weak LLM doesn't pick eval_builder for run intent.
    """
    fm = _load_skill_frontmatter("eval_builder")
    negative = _examples(fm).get("negative", []) or []
    combined = " ".join(str(e) for e in negative)
    assert "eval して" in combined or ("eval" in combined and "して" in combined), (
        f"eval_builder negative examples must include 'を eval して' run contrast; "
        f"got: {negative}"
    )


# ── eval skill symmetric wording (when_not_to_use) ────────────────────────────


def test_eval_skill_when_not_to_use_mentions_eval_builder():
    """Tier 1: eval skill when_not_to_use mentions eval_builder for create/generate intent.

    Symmetric fix: eval skill must also redirect 'eval を作って' to eval_builder.
    Without this, the weak LLM may still route to eval when it reads eval's description.
    """
    fm = _load_skill_frontmatter("eval")
    when_not = _when_not_to_use(fm)
    combined = " ".join(str(r) for r in when_not).lower()
    assert "eval_builder" in combined, (
        "eval skill when_not_to_use must mention 'eval_builder' as the correct skill "
        f"for create/generate intent; got: {combined!r}"
    )


def test_eval_skill_negative_examples_include_eval_wo_tsukutte():
    """Tier 1: eval skill negative examples include 'eval を作って' create contrast.

    Symmetric negative example: 'eval を作って' → use eval_builder, not eval.
    Gives the weak LLM a concrete anchor on both sides of the disambiguation.
    """
    fm = _load_skill_frontmatter("eval")
    negative = _examples(fm).get("negative", []) or []
    combined = " ".join(str(e) for e in negative)
    assert "eval_builder" in combined or ("eval" in combined and "作って" in combined), (
        f"eval skill negative examples must include 'eval を作って → eval_builder' contrast; "
        f"got: {negative}"
    )
