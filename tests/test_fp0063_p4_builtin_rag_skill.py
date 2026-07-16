"""Tier 2: OS invariant — FP-0063 P4: the `build_and_query_rag_corpus` builtin
skill, the "when/how to run the two pipelines" surface proposal 0063's
Architecture section names (docs/deep-dives/proposals/0063-builtin-turnkey-user-rag.md).

What this file pins, and what it deliberately does NOT:

  1. **It ships builtin + inert-but-reachable** — `provenance="builtin"`,
     `visibility="on_demand"`, `enabled=True` (A3 as corrected by #2971).
  2. **The SKILL.md is well-formed**, and its frontmatter `name` matches the
     `BUILTIN_SKILLS` key — the registry never reads the body, so nothing else
     would catch a mismatch between the two homes of that name.
  3. **Every pipeline global name the skill's prose tells the model to invoke
     really resolves through the REAL pipeline registry.** This is the pin that
     earns its place: the skill's whole job is to route the model to
     `rag_ingest.ingest` / `rag_query.query`, so a name that drifts from the
     shipped registration turns the skill from "help" into a `run_pipeline`
     failure the model cannot diagnose. Generalizes the flagship's
     single-home-naming idiom (`test_0060_phase2_f3b_builtin_content.py::
     test_flagship_pipeline_registers_under_its_namespaced_global_name`) by
     EXTRACTING the names from the prose rather than restating them here — a
     restated constant would drift with the prose and never go red.
  4. **Every repo-relative doc path the skill points at exists.** A skill whose
     pointers 404 is the "reachable but useless" state.

**Reachability is NOT re-tested here.** The `skill_list` -> path -> real `file`
read chain already iterates EVERY builtin skill in
`test_2971_skill_visibility.py` (`test_builtin_skills_ship_on_demand_and_are_listed`
+ `test_builtin_skill_body_is_readable_at_the_path_skill_list_returns`), so this
skill is covered there by construction the moment it enters `BUILTIN_SKILLS`.
Duplicating it here would add a second home for the same assert without adding
a second witness.

No mocks: the real `BUILTIN_SKILLS` map, the real `build_builtin_config`, the
real `build_pipeline_registry`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from reyn.builtin.registry import BUILTIN_SKILLS, build_builtin_config
from reyn.data.pipelines.registry import build_pipeline_registry

_REPO_ROOT = Path(__file__).parent.parent
_SKILL_NAME = "build_and_query_rag_corpus"
_SKILL_PATH = Path(BUILTIN_SKILLS[_SKILL_NAME]["path"])


def _skill_body() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. ships builtin + inert
# ---------------------------------------------------------------------------


def test_rag_skill_ships_builtin_provenance_and_inert() -> None:
    """Tier 2: the build_and_query_rag_corpus skill loads with
    provenance="builtin", visibility="on_demand" (#2971: out of the L1 menu,
    reachable via skill_list), enabled=True."""
    entry = build_builtin_config()["skills"]["entries"][_SKILL_NAME]
    assert entry["provenance"] == "builtin"
    assert entry["visibility"] == "on_demand"
    assert entry.get("enabled", True) is True


# ---------------------------------------------------------------------------
# 2. well-formed SKILL.md
# ---------------------------------------------------------------------------


def test_rag_skill_frontmatter_is_well_formed_and_name_matches_registry() -> None:
    """Tier 2: the SKILL.md frontmatter is valid YAML with `name`/`description`,
    and `name` matches the BUILTIN_SKILLS key. The registry never reads the
    body, so these two homes of the name can only be kept honest by asserting
    they agree."""
    match = re.match(r"^---\n(.*?)\n---\n", _skill_body(), re.DOTALL)
    assert match is not None, "SKILL.md must open with a YAML frontmatter block"
    frontmatter = yaml.safe_load(match.group(1))

    assert frontmatter["name"] == _SKILL_NAME
    assert isinstance(frontmatter["description"], str) and frontmatter["description"]


# ---------------------------------------------------------------------------
# 3. the pipelines the skill routes to really register under those names
# ---------------------------------------------------------------------------


def _pipeline_names_referenced_by_the_skill() -> "set[str]":
    """Extract every `run_pipeline(name="X.Y", ...)` target from the skill's
    prose. Extracted, never restated: a hardcoded list here would drift with
    the prose it claims to guard and stay green while doing it."""
    return set(re.findall(r'run_pipeline\(name="([\w.]+)"', _skill_body()))


def test_every_pipeline_the_skill_names_resolves_in_the_real_registry() -> None:
    """Tier 2: each pipeline global name the skill tells the model to invoke
    resolves through the REAL pipeline registry, under that exact name
    (entry-key.declared-name namespacing). A drift here means the skill hands
    the model a `run_pipeline` call that fails on a name it cannot debug."""
    referenced = _pipeline_names_referenced_by_the_skill()
    assert referenced, (
        "fixture invariant: the skill must tell the model which pipelines to run"
    )

    cfg = build_builtin_config()
    registry = build_pipeline_registry(cfg["pipelines"], project_root=_REPO_ROOT)

    for name in sorted(referenced):
        pipeline = registry.get(name)
        assert pipeline.name == name


def test_the_skill_names_both_halves_of_the_workflow() -> None:
    """Tier 2: the skill routes to BOTH pipelines. Ingest-without-query leaves
    a corpus nobody reads; query-without-ingest names a db that does not exist.
    The two-pipeline sequence IS the knowledge this skill exists to carry
    (proposal 0063: "builtin skill: when/how to run the two pipelines"), so a
    skill naming only one is a skill that lost its subject."""
    referenced = _pipeline_names_referenced_by_the_skill()
    assert "rag_ingest.ingest" in referenced
    assert "rag_query.query" in referenced


def test_pipeline_name_resolution_is_not_vacuous() -> None:
    """Tier 2: (regrounding) the registry really rejects an unregistered name,
    so the positive test above exercises real resolution rather than a lenient
    `get` that accepts anything. Without this, a `get` that silently returned a
    stub would keep the pin green through any drift."""
    cfg = build_builtin_config()
    registry = build_pipeline_registry(cfg["pipelines"], project_root=_REPO_ROOT)

    with pytest.raises(Exception):
        registry.get("rag_ingest.no_such_pipeline")


# ---------------------------------------------------------------------------
# 4. the skill's doc pointers resolve
# ---------------------------------------------------------------------------


def test_every_doc_path_the_skill_points_at_exists() -> None:
    """Tier 2: each repo-relative `docs/...` / `src/...` path in the skill body
    exists. The skill's closing section hands the model and the operator
    pointers for setup and backend-swap; a pointer that 404s makes the skill
    reachable but useless."""
    referenced = set(re.findall(r"`((?:docs|src)/[\w./-]+?\.(?:md|yaml))`", _skill_body()))
    assert referenced, "fixture invariant: the skill must carry pointers"

    missing = sorted(p for p in referenced if not (_REPO_ROOT / p).exists())
    assert not missing, f"skill points at non-existent paths: {missing}"
