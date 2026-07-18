"""Tier 2: OS invariant — the `build_and_query_rag_corpus` skill, the
"when/how to run the two pipelines" surface, now shipped as part of the
builtin `rag` plugin (ADR 0064 P5, `src/reyn/builtin/plugins/rag/skills/`;
originally authored under FP-0063 P4,
docs/deep-dives/proposals/0063-builtin-turnkey-user-rag.md).

Under ADR 0064 the skill is no longer a standing `BUILTIN_SKILLS` entry —
it is registered only once `plugin_install(source={"kind": "builtin",
"name": "rag"})` runs (real coverage of THAT mechanism lives in
`tests/test_plugin_install.py` + `scripts/wheel_plugin_install_probe.py`).
What this file pins instead, directly against the shipped plugin files:

  1. **The manifest declares the skills capability**, and the SKILL.md this
     file describes actually exists at the layout `plugin_install`'s
     discovery convention expects (`skills/<name>/SKILL.md`).
  2. **The SKILL.md is well-formed**, and its frontmatter `name` matches its
     own directory name — the discovery convention keys a skill by its dir
     name (empty `entries` in the manifest = "discover every
     `skills/*/SKILL.md`"), so a frontmatter/dirname mismatch is a silent
     identity split nothing else would catch.
  3. **Every pipeline global name the skill's prose tells the model to
     invoke really resolves through the REAL pipeline registry** (built
     against the plugin's own `pipelines/*.yaml`, the same files
     `plugin_install`'s pipelines capability would register). This is the
     pin that earns its place: the skill's whole job is to route the model
     to `rag_ingest.ingest` / `rag_query.query`, so a name that drifts from
     the shipped pipeline files turns the skill from "help" into a
     `run_pipeline` failure the model cannot diagnose. Names are EXTRACTED
     from the prose, never restated — a restated constant would drift with
     the prose and never go red.
  4. **Every repo-relative doc path the skill points at exists.** A skill
     whose pointers 404 is the "reachable but useless" state.

No mocks: the real `PluginManifest`, the real skill/pipeline files, the real
`build_pipeline_registry`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from reyn.data.pipelines.registry import build_pipeline_registry
from reyn.plugins.manifest import load_plugin_manifest

_REPO_ROOT = Path(__file__).parent.parent
_PLUGIN_DIR = _REPO_ROOT / "src" / "reyn" / "builtin" / "plugins" / "rag"
_SKILL_NAME = "build_and_query_rag_corpus"
_SKILL_PATH = _PLUGIN_DIR / "skills" / _SKILL_NAME / "SKILL.md"
_INGEST_PATH = _PLUGIN_DIR / "pipelines" / "rag_ingest.yaml"
_QUERY_PATH = _PLUGIN_DIR / "pipelines" / "rag_query.yaml"


def _skill_body() -> str:
    return _SKILL_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. the manifest declares the skill's layout
# ---------------------------------------------------------------------------


def test_rag_plugin_manifest_declares_skills_capability_and_the_skill_exists() -> None:
    """Tier 2: the plugin manifest declares a `skills` capability, and the
    `build_and_query_rag_corpus` SKILL.md really exists at the layout
    `plugin_install`'s discovery convention expects."""
    manifest = load_plugin_manifest(_PLUGIN_DIR)
    assert "skills" in manifest.capability_kinds
    assert _SKILL_PATH.is_file(), f"expected a SKILL.md at {_SKILL_PATH}"


# ---------------------------------------------------------------------------
# 2. well-formed SKILL.md
# ---------------------------------------------------------------------------


def test_rag_skill_frontmatter_is_well_formed_and_name_matches_dirname() -> None:
    """Tier 2: the SKILL.md frontmatter is valid YAML with `name`/`description`,
    and `name` matches its own directory name (the discovery-by-dirname key
    `plugin_install`'s empty-entries convention uses)."""
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


def _plugin_pipeline_registry():
    cfg = {
        "entries": {
            "rag_ingest": {"path": str(_INGEST_PATH)},
            "rag_query": {"path": str(_QUERY_PATH)},
        },
    }
    return build_pipeline_registry(cfg, project_root=_REPO_ROOT)


def test_every_pipeline_the_skill_names_resolves_in_the_real_registry() -> None:
    """Tier 2: each pipeline global name the skill tells the model to invoke
    resolves through the REAL pipeline registry (built against the plugin's
    own shipped pipeline files), under that exact name (entry-key.declared-
    name namespacing). A drift here means the skill hands the model a
    `run_pipeline` call that fails on a name it cannot debug."""
    referenced = _pipeline_names_referenced_by_the_skill()
    assert referenced, (
        "fixture invariant: the skill must tell the model which pipelines to run"
    )

    registry = _plugin_pipeline_registry()

    for name in sorted(referenced):
        pipeline = registry.get(name)
        assert pipeline.name == name


def test_the_skill_names_both_halves_of_the_workflow() -> None:
    """Tier 2: the skill routes to BOTH pipelines. Ingest-without-query leaves
    a corpus nobody reads; query-without-ingest names a db that does not exist.
    The two-pipeline sequence IS the knowledge this skill exists to carry, so
    a skill naming only one is a skill that lost its subject."""
    referenced = _pipeline_names_referenced_by_the_skill()
    assert "rag_ingest.ingest" in referenced
    assert "rag_query.query" in referenced


def test_pipeline_name_resolution_is_not_vacuous() -> None:
    """Tier 2: (regrounding) the registry really rejects an unregistered name,
    so the positive test above exercises real resolution rather than a lenient
    `get` that accepts anything. Without this, a `get` that silently returned a
    stub would keep the pin green through any drift."""
    registry = _plugin_pipeline_registry()

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
