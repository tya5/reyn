"""Tier 2: index_docs stdlib skill contract tests (ADR-0033 §2.1).

Covers:
  - skill.md parses and compiles without errors
  - entry_phase, graph, final_output_name correctness
  - Skill.postprocessor steps = python (extract_and_split) → python
    (write_chunks_with_lock, which embeds+indexes provider-direct) — #1303 S-I
  - Permissions declare python/trusted for all three chunker functions
  - input_schema validates correctly (required fields, mode default)
  - chunk_strategy artifact declares passthrough + strategy fields
  - index_summary artifact (postprocessor output) has required fields

No mocks; uses real load_dsl_skill compilation path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.compiler.loader import load_dsl_skill
from reyn.schemas.models import Postprocessor, PythonStep, Skill

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SKILL_MD_PATH = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "index_docs" / "skill.md"
)
_SKILL_ROOT = _SKILL_MD_PATH.parent.parent.parent  # src/reyn/stdlib/


def _load() -> Skill:
    """Load and compile the index_docs skill.md."""
    return load_dsl_skill(_SKILL_MD_PATH, skill_root=_SKILL_ROOT)


# ---------------------------------------------------------------------------
# Tier 2: skill compilation
# ---------------------------------------------------------------------------


def test_index_docs_skill_md_exists():
    """Tier 2: index_docs skill.md exists on disk."""
    assert _SKILL_MD_PATH.exists(), f"index_docs skill.md not found: {_SKILL_MD_PATH}"


def test_index_docs_skill_loads_without_error():
    """Tier 2: index_docs skill.md compiles without errors."""
    skill = _load()
    assert skill is not None
    assert isinstance(skill, Skill)


def test_index_docs_skill_name():
    """Tier 2: skill.name == 'index_docs'."""
    assert _load().name == "index_docs"


def test_index_docs_entry_phase():
    """Tier 2: entry_phase == 'strategy'."""
    assert _load().entry_phase == "strategy"


def test_index_docs_final_output_name():
    """Tier 2: final_output_name == 'chunk_strategy' (LLM contract artifact)."""
    assert _load().final_output_name == "chunk_strategy"


# ---------------------------------------------------------------------------
# Tier 2: phase and graph
# ---------------------------------------------------------------------------


def test_index_docs_strategy_phase_exists():
    """Tier 2: 'strategy' phase is present in skill.phases."""
    skill = _load()
    assert "strategy" in skill.phases


def test_index_docs_strategy_phase_can_finish():
    """Tier 2: strategy phase appears in graph.can_finish_phases."""
    skill = _load()
    assert "strategy" in skill.graph.can_finish_phases


def test_index_docs_graph_single_phase():
    """Tier 2: graph has no transitions from strategy (single-phase skill)."""
    skill = _load()
    transitions = skill.graph.transitions.get("strategy", [])
    assert transitions == [], f"Expected no transitions, got: {transitions}"


# ---------------------------------------------------------------------------
# Tier 2: strategy phase preprocessor
# ---------------------------------------------------------------------------


def test_index_docs_strategy_preprocessor_has_two_python_steps():
    """Tier 2: strategy phase preprocessor has exactly 2 python steps (gather_samples + cost_preflight)."""
    skill = _load()
    phase = skill.phases["strategy"]
    steps = phase.preprocessor
    assert steps, "expected preprocessor steps to be present"
    assert all(isinstance(s, PythonStep) for s in steps)
    # Verify both required steps are present by name
    fn_names = [s.function for s in steps]
    assert "gather_samples" in fn_names
    assert "cost_preflight" in fn_names


def test_index_docs_strategy_preprocessor_step_functions():
    """Tier 2: strategy preprocessor steps call gather_samples then cost_preflight."""
    skill = _load()
    phase = skill.phases["strategy"]
    fns = [s.function for s in phase.preprocessor]
    assert fns == ["gather_samples", "cost_preflight"]


def test_index_docs_strategy_preprocessor_into_paths():
    """Tier 2: gather_samples result placed at data.samples_result; cost at data.cost."""
    skill = _load()
    phase = skill.phases["strategy"]
    assert phase.preprocessor[0].into == "data.samples_result"
    assert phase.preprocessor[1].into == "data.cost"


# ---------------------------------------------------------------------------
# Tier 2: input schema (index_docs_input)
# ---------------------------------------------------------------------------


def test_index_docs_strategy_input_schema_has_required_fields():
    """Tier 2: strategy phase input_schema requires source, path, description."""
    skill = _load()
    phase = skill.phases["strategy"]
    schema = phase.input_schema
    # The schema wraps index_docs_input artifact in {type, data} envelope
    data_props = schema.get("properties", {}).get("data", {}).get("properties", {})
    assert "source" in data_props, f"'source' missing from data properties: {list(data_props)}"
    assert "path" in data_props
    assert "description" in data_props
    assert "mode" in data_props


def test_index_docs_strategy_input_schema_mode_has_enum():
    """Tier 2: mode field has enum [append, replace]."""
    skill = _load()
    phase = skill.phases["strategy"]
    schema = phase.input_schema
    data_props = schema.get("properties", {}).get("data", {}).get("properties", {})
    mode_schema = data_props.get("mode", {})
    enum_values = mode_schema.get("enum", [])
    assert set(enum_values) == {"append", "replace"}


# ---------------------------------------------------------------------------
# Tier 2: postprocessor structure
# ---------------------------------------------------------------------------


def test_index_docs_has_postprocessor():
    """Tier 2: skill.postprocessor is not None."""
    skill = _load()
    assert skill.postprocessor is not None
    assert isinstance(skill.postprocessor, Postprocessor)


def test_index_docs_postprocessor_output_name():
    """Tier 2: postprocessor.output_name == 'index_summary'."""
    skill = _load()
    assert skill.postprocessor.output_name == "index_summary"


def test_index_docs_postprocessor_no_run_ops():
    """Tier 2: the postprocessor has no run_op steps — #1303 Stage I folded the
    embed + index_write run-ops into the write_chunks_with_lock python step,
    which streams chunks into reyn.safe.embed_index.
    """
    skill = _load()
    steps = skill.postprocessor.steps
    assert steps, "expected postprocessor steps to be present"
    # The cutover removed both run_op steps; only safe-python steps remain.
    assert all(s.type == "python" for s in steps), "no run_op steps should remain"
    fns = [s.function for s in steps if isinstance(s, PythonStep)]
    assert "extract_and_split" in fns
    assert "write_chunks_with_lock" in fns


def test_index_docs_postprocessor_step0_is_extract_and_split():
    """Tier 2: postprocessor step[0] calls extract_and_split (safe, glob enum)."""
    skill = _load()
    step = skill.postprocessor.steps[0]
    assert isinstance(step, PythonStep)
    assert step.function == "extract_and_split"
    assert step.into == "data.chunk_list"


def test_index_docs_postprocessor_step1_is_write_chunks_with_lock():
    """Tier 2: postprocessor step[1] calls write_chunks_with_lock — the safe
    step that now also embeds + indexes (streams to reyn.safe.embed_index)."""
    skill = _load()
    step = skill.postprocessor.steps[1]
    assert isinstance(step, PythonStep)
    assert step.function == "write_chunks_with_lock"
    assert step.into == "data.chunk_stats"


# ---------------------------------------------------------------------------
# Tier 2: permissions
# ---------------------------------------------------------------------------


def test_index_docs_permissions_python_modes_declared():
    """Tier 2: skill declares the expected mode per chunker function.

    Post-FP-0042 Phase 2.8 (= ``apply_strategy`` retired):
      - gather_samples / cost_preflight: safe (chunkers_preproc_safe.py)
      - extract_and_split / write_chunks_with_lock: safe (chunkers_safe.py)
      - apply_strategy: REMOVED — was the last grandfathered exemption.
    """
    skill = _load()
    python_perms = skill.permissions.python
    assert python_perms, "expected python permissions to be declared"

    fn_modes = {p.function: p.mode for p in python_perms}
    assert fn_modes.get("gather_samples") == "safe"
    assert fn_modes.get("cost_preflight") == "safe"
    assert fn_modes.get("extract_and_split") == "safe"
    assert fn_modes.get("write_chunks_with_lock") == "safe"
    assert "apply_strategy" not in fn_modes, (
        "apply_strategy was retired in FP-0042 Phase 2.8 — its skill.md "
        "permission entry must stay removed."
    )


def test_index_docs_permissions_python_module_is_relative_path():
    """Tier 2: python permission module paths are relative (./chunkers.py)."""
    skill = _load()
    for perm in skill.permissions.python:
        assert perm.module.startswith("./"), (
            f"Expected relative module path, got: {perm.module!r}"
        )


# ---------------------------------------------------------------------------
# Tier 2: chunk_strategy artifact schema
# ---------------------------------------------------------------------------


def test_index_docs_chunk_strategy_artifact_schema_has_strategy_fields():
    """Tier 2: chunk_strategy artifact schema includes boundary + max_chunk_size_tokens."""
    skill = _load()
    # final_output_schema wraps chunk_strategy in {type, data}
    schema = skill.final_output_schema
    data_props = schema.get("properties", {}).get("data", {}).get("properties", {})
    assert "boundary" in data_props, f"'boundary' missing: {list(data_props)}"
    assert "max_chunk_size_tokens" in data_props


def test_index_docs_chunk_strategy_artifact_schema_has_passthrough_fields():
    """Tier 2: chunk_strategy artifact schema includes passthrough fields (source, path, description, mode)."""
    skill = _load()
    schema = skill.final_output_schema
    data_props = schema.get("properties", {}).get("data", {}).get("properties", {})
    for field in ("source", "path", "description", "mode"):
        assert field in data_props, (
            f"Passthrough field '{field}' missing from chunk_strategy artifact schema"
        )


def test_index_docs_chunk_strategy_boundary_has_enum():
    """Tier 2: boundary field has enum [heading, blank_line, sentence]."""
    skill = _load()
    schema = skill.final_output_schema
    data_props = schema.get("properties", {}).get("data", {}).get("properties", {})
    boundary = data_props.get("boundary", {})
    assert set(boundary.get("enum", [])) == {"heading", "blank_line", "sentence"}


# ---------------------------------------------------------------------------
# Tier 2: index_summary artifact (postprocessor output)
# ---------------------------------------------------------------------------


def test_index_docs_postprocessor_output_schema_has_required_fields():
    """Tier 2: postprocessor output_schema includes source + the chunk_stats
    result group.

    #1303 Stage I: the embed + index_write run-ops folded into
    write_chunks_with_lock, so all run counts live under one chunk_stats group
    (the separate embed_result / index_result groups are gone).
    """
    skill = _load()
    schema = skill.postprocessor.output_schema
    if "properties" in schema and "data" in schema.get("properties", {}):
        data_props = schema["properties"]["data"].get("properties", {})
    else:
        data_props = schema.get("properties", {})

    for field in ("source", "chunk_stats"):
        assert field in data_props, (
            f"Required field '{field}' missing from postprocessor output schema"
        )
    # The old run-op result groups are gone.
    assert "embed_result" not in data_props
    assert "index_result" not in data_props

    chunk_stats_props = data_props["chunk_stats"].get("properties", {})
    for f in ("chunk_count", "embedded", "skipped_embed", "written"):
        assert f in chunk_stats_props, f"chunk_stats.{f} missing"
