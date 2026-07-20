"""Tier 2: OS invariant — the `build_and_query_rag_corpus` skill, the RAG
plugin's routing + "how to actually call the two pipelines" surface, shipped
as part of the builtin `rag` plugin (ADR 0064 P5,
`src/reyn/builtin/plugins/rag/skills/`; originally authored under FP-0063 P4,
docs/deep-dives/proposals/0063-builtin-turnkey-user-rag.md).

#3162 part 1 split the original single `build_and_query_rag_corpus` skill
(21_837 bytes -- 266% of the default 8_192-char `read_file` inline cap,
already silently truncated in practice for any caller without a
large-window model resolved) into five smaller sibling skills. A later pass
of #3162 folded those five back into the STANDARD Agent Skills shape (one
skill directory = `SKILL.md` router + bundled `references/*.md` — the five
sibling skills were never the standard form): routing + install stays in
`SKILL.md` itself; the two `pipeline__run` calls this file pins moved to
`references/run-ingest-and-query-workflow.md`; embedding setup moved to
`references/configure-embedding-provider.md` /
`references/configure-local-embedding-model.md`; schema/tuning/backend-swap
moved to `references/corpus-internals-schema-tuning-and-backend-swap.md`.
This file's target and docstring were updated in the same PR to track that
move — `_skill_body()` below now concatenates `SKILL.md` with every bundled
`references/*.md` file, so the extraction-based checks (pipeline names, doc
paths, tool names) keep pinning the SAME prose regardless of which file
within the one skill directory it physically lives in
(`tests/test_skill_md_default_inline_cap_gate.py` is the structural gate
keeping every file under the cap;
`tests/test_skill_references_gate_3162.py` gates the reference-link
mechanism itself).

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
     `plugin_management__install`'s pipelines capability would register).
     This is the pin that earns its place: the skill's whole job is to
     route the model to `rag_ingest.ingest` / `rag_query.query`, so a name
     that drifts from the shipped pipeline files turns the skill from
     "help" into a `pipeline__run` failure the model cannot diagnose.
     Names are EXTRACTED from the prose, never restated — a restated
     constant would drift with the prose and never go red.
  4. **Every repo-relative doc path the skill points at exists.** A skill
     whose pointers 404 is the "reachable but useless" state.
  5. **Every qualified TOOL name the skill's prose tells the model to call
     (`plugin_management__install(...)`, `mcp__install_local(...)`,
     `pipeline__run(...)`, ...) really exists in the REAL enumerate-all
     catalog** (`catalog_entries(ctx)` — the single source `list_actions` /
     `describe_action` / the live `tools=` payload all agree against,
     #1455). #3090: this is the pin the repro was missing — SKILL.md
     taught `run_pipeline(...)` / `plugin_install(...)`, names that never
     existed under enumerate-all (the real names are `pipeline__run` /
     `plugin_management__install`); a weak model given no matching tool
     looped 24x on the nearest-spelled wrong one
     (`pipeline__run_inline`) and never recovered. Names are EXTRACTED
     from the skill's code fences, never restated, for the same
     never-goes-red reason as (3). #3092 generalized THIS pin to every
     builtin SKILL.md (`tests/test_builtin_skill_tool_name_drift_3092.py`)
     after finding the exact same drift shape on two skills the RAG-only
     scope here could not see; the extraction/lookup logic below is now
     SHARED with that gate via `tests/_support/builtin_skill_tool_names.py`
     so the two checks cannot silently diverge.

No mocks: the real `PluginManifest`, the real skill/pipeline files, the real
`build_pipeline_registry`, the real `catalog_entries`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from reyn.data.pipelines.registry import build_pipeline_registry
from reyn.plugins.manifest import load_plugin_manifest
from tests._support.builtin_skill_tool_names import (
    qualified_tool_names_referenced,
    real_catalog_tool_names,
)

_REPO_ROOT = Path(__file__).parent.parent
_PLUGIN_DIR = _REPO_ROOT / "src" / "reyn" / "builtin" / "plugins" / "rag"
_SKILL_NAME = "build_and_query_rag_corpus"
_SKILL_DIR = _PLUGIN_DIR / "skills" / _SKILL_NAME
_SKILL_PATH = _SKILL_DIR / "SKILL.md"
_INGEST_PATH = _PLUGIN_DIR / "pipelines" / "rag_ingest.yaml"
_QUERY_PATH = _PLUGIN_DIR / "pipelines" / "rag_query.yaml"


def _skill_body() -> str:
    """`SKILL.md` concatenated with every bundled `references/*.md` file —
    post-consolidation (#3162), the prose this file's extraction-based
    checks pin (pipeline names, doc paths, tool names) is spread across the
    router and its references rather than confined to one file, so the
    checks must see the whole skill directory to keep pinning the same
    content they pinned before the five-skill split was folded back in."""
    parts = [_SKILL_PATH.read_text(encoding="utf-8")]
    references_dir = _SKILL_DIR / "references"
    if references_dir.is_dir():
        for path in sorted(references_dir.glob("*.md")):
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


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
    `plugin_install`'s empty-entries convention uses). Checked against
    `SKILL.md` alone (not `_skill_body()`'s references-concatenated form) —
    only `SKILL.md` carries frontmatter; a bundled `references/*.md` file is
    a plain leaf with none (see `test_skill_references_gate_3162.py`)."""
    skill_md_only = _SKILL_PATH.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", skill_md_only, re.DOTALL)
    assert match is not None, "SKILL.md must open with a YAML frontmatter block"
    frontmatter = yaml.safe_load(match.group(1))

    assert frontmatter["name"] == _SKILL_NAME
    assert isinstance(frontmatter["description"], str) and frontmatter["description"]


# ---------------------------------------------------------------------------
# 3. the pipelines the skill routes to really register under those names
# ---------------------------------------------------------------------------


def _pipeline_names_referenced_by_the_skill() -> "set[str]":
    """Extract every `pipeline__run(name="X.Y", ...)` target from the skill's
    prose. Extracted, never restated: a hardcoded list here would drift with
    the prose it claims to guard and stay green while doing it."""
    return set(re.findall(r'pipeline__run\(name="([\w.]+)"', _skill_body()))


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
    `pipeline__run` call that fails on a name it cannot debug."""
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


# ---------------------------------------------------------------------------
# 5. every qualified TOOL name the skill calls really exists in the catalog
#    (#3090 doc-code sync gate)
# ---------------------------------------------------------------------------


def _qualified_tool_names_referenced_by_the_skill() -> "set[str]":
    """Extract every qualified (`category__verb`) tool-call name from the
    skill's code fences — `plugin_management__install(...)`,
    `mcp__install_local(...)`, `pipeline__run(...)`. Delegates to the SHARED
    extractor (`tests/_support/builtin_skill_tool_names.py`) the #3092
    generalized gate also uses, so this skill's own check and the
    all-builtin-skills check can never silently diverge. See that module's
    docstring for the REACH LIMIT (CALL-shape only, bare mentions invisible
    by design — #3091 review found a bare `plugin_install` this gate cannot
    see; widening to bare mentions is NOT the fix)."""
    return qualified_tool_names_referenced(_skill_body())


def test_every_tool_name_the_skill_calls_exists_in_the_real_catalog() -> None:
    """Tier 2: (#3090) every qualified tool name SKILL.md tells the model to
    call must be a name the REAL enumerate-all catalog actually enumerates
    (`catalog_entries(ctx)` — the single source `tools=` is built from, #1455
    list == describe == dispatch). #3090's root cause was exactly this gap:
    SKILL.md taught `run_pipeline(...)` / `plugin_install(...)`, names that
    do not exist under enumerate-all (the real names are `pipeline__run` /
    `plugin_management__install`) — a weak model given a `tools=` payload
    with no matching entry could not find the right tool, called the
    nearest-spelled wrong one (`pipeline__run_inline`) 24 times running, and
    never recovered even after two explicit corrections. This test fails
    LOUD the moment a future tool rename makes SKILL.md drift the same way
    again."""
    referenced = _qualified_tool_names_referenced_by_the_skill()
    assert referenced, "fixture invariant: the skill must call qualified tools"

    real_names = real_catalog_tool_names()
    missing = sorted(n for n in referenced if n not in real_names)
    assert not missing, (
        f"SKILL.md calls tool name(s) that do not exist in the real "
        f"enumerate-all catalog: {missing} (real catalog has: "
        f"{sorted(n for n in real_names if '__' in n)})"
    )


def test_tool_name_catalog_check_is_not_vacuous() -> None:
    """Tier 2: (regrounding, strip-falsify) inject a tool name into the
    skill's prose that does NOT exist in the real catalog (the exact #3090
    shape — `run_pipeline`) and confirm the check above actually goes RED.
    Without this, a `real_names` set that silently matched anything (an
    empty exclusion, a substring check, an over-permissive regex) would
    keep the positive test green through any drift and make the whole gate
    decorative."""
    referenced = _qualified_tool_names_referenced_by_the_skill() | {
        "run_pipeline__ghost",
    }
    real_names = real_catalog_tool_names()
    missing = sorted(n for n in referenced if n not in real_names)
    assert missing == ["run_pipeline__ghost"]
