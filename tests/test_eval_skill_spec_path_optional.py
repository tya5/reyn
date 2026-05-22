"""Tier 2: eval skill output schema does NOT require ``spec_path``.

Pinned invariants:

- ``eval_result_raw`` artifact schema accepts payloads that omit
  ``spec_path`` (e.g. when the LLM was given a scenario user-prompt
  with no spec.md reference). Before the fix, the LLM emitted
  ``spec_path: null`` → schema validation rejected the whole eval
  output → ``skill_run_failed``. Observed B43/B44/B45/B46 = 4
  consecutive dogfood batches.
- The caller-facing ``eval_result`` final_output schema in skill.md
  matches: ``spec_path`` is a pass-through reference field, not a
  required computation field. Postprocessor (``compute_eval_score``)
  does not read it — verified by inspecting ``postprocessor.py``.
- Other required fields stay required (= regression guard against
  accidentally dropping ``summary`` / ``criteria_results`` etc.).

testing.ja.md compliance:
- No mocks. Schema YAML is parsed directly with ``yaml.safe_load``.
- No private-state assertions.
- No algorithm pinning — the test verifies *which fields are
  required*, not implementation details of how the schema is
  enforced.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EVAL_SKILL_DIR = _REPO_ROOT / "src" / "reyn" / "stdlib" / "skills" / "eval"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _parse_skill_md_frontmatter(skill_md_path: Path) -> dict:
    """Read the YAML frontmatter from skill.md (= between the leading
    --- markers)."""
    text = skill_md_path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    # Frontmatter is parts[1] (parts[0] is "" before the first ---).
    return yaml.safe_load(parts[1])


# ---------------------------------------------------------------------------
# eval_result_raw schema
# ---------------------------------------------------------------------------


def test_eval_result_raw_does_not_require_spec_path():
    """Tier 2 (B46-fix): the LLM-authored raw artifact schema must
    NOT require ``spec_path``. The field stays in ``properties`` (=
    LLM may still pass it through when known) but is no longer
    required for validation to pass."""
    raw_yaml = _load_yaml(_EVAL_SKILL_DIR / "artifacts" / "eval_result_raw.yaml")
    required = raw_yaml["schema"].get("required", [])
    assert "spec_path" not in required, (
        f"spec_path must NOT be in eval_result_raw.required. "
        f"Currently: {required}. See B43-B46 retrospective — eval "
        f"skill_run_failed in 4 consecutive batches because LLM "
        f"emitted spec_path=null when the scenario user-prompt did "
        f"not supply a spec path."
    )
    # Field still discoverable to the LLM (= LLM can fill it when known).
    assert "spec_path" in raw_yaml["schema"]["properties"]


def test_eval_result_raw_keeps_other_required_fields():
    """Tier 2: regression guard — relaxing spec_path must not
    accidentally drop other required fields. ``criteria_results``,
    ``weakest_phase``, ``summary``, ``run_status`` remain required
    because the postprocessor and caller-facing contract depend on
    them."""
    raw_yaml = _load_yaml(_EVAL_SKILL_DIR / "artifacts" / "eval_result_raw.yaml")
    required = set(raw_yaml["schema"].get("required", []))
    for field in ("criteria_results", "weakest_phase", "summary", "run_status"):
        assert field in required, (
            f"{field} must remain required in eval_result_raw — "
            f"postprocessor / caller-facing contract depends on it. "
            f"Currently required: {required}"
        )


# ---------------------------------------------------------------------------
# skill.md final_output_schema + postprocessor python step output_schema
# ---------------------------------------------------------------------------


def test_skill_md_final_output_does_not_require_spec_path():
    """Tier 2: caller-facing eval_result schema must NOT require
    spec_path. Matches the eval_result_raw relaxation — spec_path is
    pass-through, not load-bearing for scoring.

    B48-NF-W2-S5 fix (2026-05-22): skill.md's
    ``postprocessor.output_schema`` is now a string reference to the
    ``eval_result`` artifact (so the compiler wraps it into a
    ``{type, data}`` envelope). The schema itself lives in
    ``artifacts/eval_result.yaml`` — read it through that indirection.
    """
    fm = _parse_skill_md_frontmatter(_EVAL_SKILL_DIR / "skill.md")
    ref = fm["postprocessor"]["output_schema"]
    assert isinstance(ref, str) and ref == "eval_result", (
        f"postprocessor.output_schema must reference the eval_result "
        f"artifact by name (= envelope-wrapped via compiler). "
        f"Currently: {ref!r}"
    )
    art = _load_yaml(_EVAL_SKILL_DIR / "artifacts" / "eval_result.yaml")
    schema = art["schema"]
    required = schema.get("required", [])
    assert "spec_path" not in required, (
        f"eval_result.schema.required must NOT include spec_path. "
        f"Currently: {required}"
    )
    # The field stays declared (= callers that pass it through still
    # see it in the schema).
    assert "spec_path" in schema["properties"]


def test_skill_md_postprocessor_step_does_not_require_spec_path():
    """Tier 2: the python postprocessor step's output_schema must
    also drop spec_path from required — it would otherwise fail
    independently of the final_output check above. Belt-and-braces:
    both schemas must agree."""
    fm = _parse_skill_md_frontmatter(_EVAL_SKILL_DIR / "skill.md")
    steps = fm["postprocessor"]["steps"]
    py_step = next(s for s in steps if s["type"] == "python")
    required = py_step["output_schema"].get("required", [])
    assert "spec_path" not in required, (
        f"postprocessor step output_schema.required must NOT include "
        f"spec_path. Currently: {required}"
    )


def test_compiled_postprocessor_output_schema_is_envelope_shaped():
    """Tier 2 (B48-NF-W2-S5 fix, 2026-05-22): the compiled
    ``postprocessor.output_schema`` must be the full
    ``{type, data}`` envelope so PostprocessorExecutor.run()'s final
    validation (which inspects the envelope-shaped result) succeeds.

    Before the fix, skill.md declared the schema inline as a flat dict
    with ``required: [passed, overall_score, ...]`` at top level. The
    compiler used the literal as-is, so the executor's envelope-vs-flat
    mismatch caused every postprocessor run to fail with
    ``'overall_score' is a required property`` (Mode F W2-S5 in B48,
    3/3 deterministic reproduction).

    Pinned invariant: compiled top-level required must be
    ``[type, data]`` (= envelope-wrapped), AND ``properties.type.const``
    must be ``eval_result``.
    """
    from reyn.compiler import load_dsl_skill
    skill = load_dsl_skill(
        str(_EVAL_SKILL_DIR / "skill.md"),
        skill_root=str(_EVAL_SKILL_DIR),
    )
    schema = skill.postprocessor.output_schema
    required = schema.get("required", [])
    assert set(required) == {"type", "data"}, (
        f"compiled postprocessor.output_schema must be envelope-shaped "
        f"(= top-level required == [type, data]); got {required!r}. "
        f"Likely cause: skill.md declared output_schema as an inline "
        f"dict instead of an artifact-name string reference, bypassing "
        f"the compiler's artifact_to_json_schema() envelope wrapper."
    )
    type_const = schema.get("properties", {}).get("type", {}).get("const")
    assert type_const == "eval_result", (
        f"compiled envelope schema must pin type.const == 'eval_result'; "
        f"got {type_const!r}."
    )


def test_skill_md_final_output_keeps_other_required_fields():
    """Tier 2: regression guard for the caller-facing schema.
    Scoring fields (``passed`` / ``overall_score`` / ``passed_criteria``
    / ``total_criteria``) and prose contract fields (``weakest_phase``
    / ``summary``) remain required.

    Reads through the ``eval_result.yaml`` artifact (= source of truth
    after B48-NF-W2-S5 fix; skill.md only references it by name).
    """
    art = _load_yaml(_EVAL_SKILL_DIR / "artifacts" / "eval_result.yaml")
    required = set(art["schema"].get("required", []))
    for field in (
        "passed", "overall_score", "passed_criteria", "total_criteria",
        "weakest_phase", "summary",
    ):
        assert field in required, (
            f"{field} must remain required in eval_result.schema. "
            f"Currently: {required}"
        )
