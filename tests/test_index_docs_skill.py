"""Tier 2: index_docs stdlib skill contract tests (ADR-0033 §2.1).

Covers:
  - skill.md parses and compiles without errors
  - entry_phase, graph, final_output_name correctness
  - Skill.postprocessor steps = python → embed_run_op → index_write_run_op
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
from reyn.schemas.models import Postprocessor, PythonStep, RunOpStep, Skill

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
    assert len(steps) == 2
    assert all(isinstance(s, PythonStep) for s in steps)


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


def test_index_docs_postprocessor_four_steps():
    """Tier 2: postprocessor has exactly 4 steps: python → python → run_op → run_op.

    R-PURE-MODE-REDEFINE Class A split: apply_strategy was split into
    extract_and_split (safe, step 0) + write_chunks_with_lock (unsafe, step 1).
    Steps 2 and 3 are the existing embed and index_write run_ops.
    """
    skill = _load()
    steps = skill.postprocessor.steps
    assert len(steps) == 4
    assert steps[0].type == "python"
    assert steps[1].type == "python"
    assert steps[2].type == "run_op"
    assert steps[3].type == "run_op"


def test_index_docs_postprocessor_step0_is_extract_and_split():
    """Tier 2: postprocessor step[0] calls extract_and_split (safe, glob enum)."""
    skill = _load()
    step = skill.postprocessor.steps[0]
    assert isinstance(step, PythonStep)
    assert step.function == "extract_and_split"
    assert step.into == "data.chunk_list"


def test_index_docs_postprocessor_step1_is_write_chunks_with_lock():
    """Tier 2: postprocessor step[1] calls write_chunks_with_lock (unsafe, minimal I/O)."""
    skill = _load()
    step = skill.postprocessor.steps[1]
    assert isinstance(step, PythonStep)
    assert step.function == "write_chunks_with_lock"
    assert step.into == "data.chunk_stats"


def test_index_docs_postprocessor_step2_is_embed_op():
    """Tier 2: postprocessor step[2] is a run_op wrapping an embed op."""
    skill = _load()
    step = skill.postprocessor.steps[2]
    assert isinstance(step, RunOpStep)
    assert step.op.kind == "embed"
    assert step.op.input_artifact == "artifacts/chunks.jsonl"
    assert step.op.output_artifact == "artifacts/chunks_with_vectors.jsonl"


def test_index_docs_postprocessor_step3_is_index_write_op():
    """Tier 2: postprocessor step[3] is a run_op wrapping an index_write op."""
    skill = _load()
    step = skill.postprocessor.steps[3]
    assert isinstance(step, RunOpStep)
    assert step.op.kind == "index_write"
    assert step.op.input_artifact == "artifacts/chunks_with_vectors.jsonl"


def test_index_docs_postprocessor_step3_args_from():
    """Tier 2: index_write run_op uses args_from to inject source + mode from artifact."""
    skill = _load()
    step = skill.postprocessor.steps[3]
    assert isinstance(step, RunOpStep)
    assert "source" in step.args_from
    assert "mode" in step.args_from
    assert step.args_from["source"] == "data.source"
    assert step.args_from["mode"] == "data.mode"


# ---------------------------------------------------------------------------
# Tier 2: permissions
# ---------------------------------------------------------------------------


def test_index_docs_permissions_python_modes_declared():
    """Tier 2: skill declares the expected mode per chunker function.

    Post-FP-0042 Phase 2.1:
      - gather_samples / cost_preflight: safe (chunkers_preproc_safe.py)
      - extract_and_split: safe (chunkers_safe.py)
      - write_chunks_with_lock: unsafe minimal — lock + content read +
        jsonl write (will migrate in Phase 2.2)
      - apply_strategy: unsafe deprecated, kept for override compat
    """
    skill = _load()
    python_perms = skill.permissions.python
    assert len(python_perms) >= 5, f"Expected at least 5 python perms, got {len(python_perms)}"

    fn_modes = {p.function: p.mode for p in python_perms}
    assert fn_modes.get("gather_samples") == "safe"
    assert fn_modes.get("cost_preflight") == "safe"
    assert fn_modes.get("extract_and_split") == "safe"
    assert fn_modes.get("write_chunks_with_lock") == "unsafe"
    assert fn_modes.get("apply_strategy") == "unsafe"


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
    """Tier 2: postprocessor output_schema includes source + nested step result groups.

    Schema updated in batch 17 dogfood (commit 0c50a20) to match actual
    postprocessor step output shape: nested chunk_stats / embed_result /
    index_result groups under data, not flat chunk_count / embedded_count.
    """
    skill = _load()
    schema = skill.postprocessor.output_schema
    if "properties" in schema and "data" in schema.get("properties", {}):
        data_props = schema["properties"]["data"].get("properties", {})
    else:
        data_props = schema.get("properties", {})

    for field in ("source", "chunk_stats", "embed_result", "index_result"):
        assert field in data_props, (
            f"Required field '{field}' missing from postprocessor output schema"
        )

    chunk_stats_props = data_props["chunk_stats"].get("properties", {})
    assert "chunk_count" in chunk_stats_props, "chunk_stats.chunk_count missing"

    embed_props = data_props["embed_result"].get("properties", {})
    assert "embedded_count" in embed_props, "embed_result.embedded_count missing"
