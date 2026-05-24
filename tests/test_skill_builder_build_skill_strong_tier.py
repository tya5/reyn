"""Tier 2: skill_builder ``build_skill`` phase declares ``model_class: strong``.

Pinned invariant:

- The ``build_skill`` phase frontmatter has ``model_class: strong``.
  This is load-bearing for skill_builder reliability under flash-lite
  defaults: the build_skill LLM must reconcile upstream design (=
  ``data.python_modules`` + per-phase preprocessor refs) into a
  consistent set of files, and must follow the in-prompt sanity check
  (lines ~170 of build_skill.md): "Each ``module`` referenced in any
  phase's preprocessor MUST appear in ``data.python_modules``. If
  not, STOP and rollback — the plan is broken."

  Flash-lite empirically fails this rule (= hallucinates a
  ``reyn_utils`` module name despite ``data.python_modules`` being
  empty, then on rollback retry produces the same broken output →
  ``phase_no_progress`` → ``workflow_aborted``).

  N=5 stability experiment (2026-05-21, both runs against same
  scenario "ウェブ記事の URL を受け取り、内容を 3 文で要約する skill"
  via A2A POST, identical user prompt):
    - all-standard (= no model_class on build_skill, flash-lite): 2/5 PASS
    - build_skill=strong (= gemini-2.5-flash on build_skill only): 3/3 PASS
      among runs where the build_skill phase actually executed (the
      other 2 runs failed earlier — design_artifacts interrupt and
      a parallel-dispatch race that prevented skill start). Among
      runs that exercised build_skill, the strong tier closed the
      ``phase_no_progress`` attractor at 100%.

  Other 4 phases (plan_skill / design_artifacts / review_plan /
  verify_skill) remain at default standard — the targeted tier
  bump is the smallest surface that closes the attractor.

testing.ja.md compliance:
- No mocks. Reads the real DSL file via ``yaml.safe_load``.
- No private-state assertions.
- Pins the exact frontmatter field that the OS reads via
  ``Phase.model_class`` (see ``runtime.py:_effective_model``).
"""
from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILD_SKILL_MD = (
    _REPO_ROOT
    / "src" / "reyn" / "stdlib" / "skills" / "skill_builder" / "phases"
    / "build_skill.md"
)


def _parse_frontmatter(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    return yaml.safe_load(parts[1])


def test_build_skill_phase_declares_model_class_strong():
    """Tier 2: ``build_skill`` phase must have ``model_class: strong`` in its frontmatter (B46-fix)."""
    fm = _parse_frontmatter(_BUILD_SKILL_MD)
    assert fm["name"] == "build_skill", (
        f"Expected build_skill phase, got {fm.get('name')!r}"
    )
    assert fm.get("model_class") == "strong", (
        f"build_skill must declare model_class: strong. Frontmatter: "
        f"{fm!r}. See test docstring for the empirical motivation "
        f"(= flash-lite phase_no_progress attractor in 60%+ of runs)."
    )


def test_skill_builder_phase_tier_assignments():
    """Tier 2: regression guard on per-phase model_class choices.

    Only ``build_skill`` and ``review_plan`` are at strong tier in
    skill_builder. design_artifacts is explicitly ``standard`` (=
    legacy explicit choice, behaviour-equivalent to default).
    plan_skill and verify_skill leave model_class unset (= runtime
    default = standard).

    Guards against:
    - Accidentally bumping additional phases to strong (= cost
      regression; future contributors must update this map with
      empirical justification).
    - Accidentally downgrading build_skill or review_plan to
      standard (= empirically PASS rate regression — see
      ``test_build_skill_phase_declares_model_class_strong`` docstring).
    """
    skill_builder_phases = _BUILD_SKILL_MD.parent
    expected: dict[str, str | None] = {
        "build_skill": "strong",
        "review_plan": "strong",
        "design_artifacts": "standard",
        "plan_skill": None,
        "verify_skill": None,
    }

    for phase_file in skill_builder_phases.glob("*.md"):
        fm = _parse_frontmatter(phase_file)
        name = fm["name"]
        actual = fm.get("model_class")
        if name in expected:
            want = expected[name]
            assert actual == want, (
                f"{name} model_class: expected {want!r}, got {actual!r}. "
                f"If this change is intentional, update the expected map "
                f"in this test with empirical justification (= N=5+ "
                f"stability data for tier changes)."
            )
