"""Tier 2: OS invariant -- builtin registry <-> disk parity gate
(``src/reyn/builtin/registry.py``).

**Motivation (the hole this closes).** ``reyn.builtin.registry`` mirrors
``reyn.hooks.schema_registry.BUILTIN_HOOK_SCHEMAS``: there is NO directory
auto-scan anywhere under ``src/reyn/builtin`` (no ``iterdir``/``glob``/
``rglob`` walks the tree) -- a builtin skill is reachable ONLY if it has an
explicit entry in ``BUILTIN_SKILLS``, because that is the ONE thing
``skill_list`` enumerates (module docstring A3: "the model finds it by
calling ``skill_list``, then reads its ``SKILL.md`` body"). A ``SKILL.md``
that ships on disk but is never added to ``BUILTIN_SKILLS`` is therefore
PERMANENTLY undiscoverable -- mechanism-exists-but-unreachable, not a
runtime error, so nothing before this gate caught it. PR #3163 hit exactly
this (a ``SKILL.md`` shipped, the registry entry was missing) and CI passed
anyway; the registry's own docstring records the earlier #2971 incident in
the same family (a force-stamped field silently made a skill undiscoverable
by any surface). This test module is the CI-enforced gate that #3163's
review caught by hand.

**Two independent properties, gated separately so a RED result names which
one broke (strip-falsify verified per-property, see PR body):**

1. **Existence parity, both directions** -- every ``BUILTIN_SKILLS`` /
   ``BUILTIN_PIPELINES`` entry's ``path`` resolves to a real file on disk
   (registry -> disk: catches a stale/typo'd path), AND every
   ``SKILL.md`` / pipeline ``*.yaml`` actually present on disk under the
   builtin tree has a corresponding registry entry (disk -> registry:
   catches the #3163 shape -- shipped but never registered). Enumeration is
   driven FROM THE REGISTRY / a real directory walk, not a hardcoded name
   list in this file, so a newly added entry or a newly added disk file is
   picked up automatically without touching this test.
2. **Description verbatim parity (skills only -- see the sibling-sweep note
   below for why pipelines/presentations are excluded)** -- each skill's
   ``SKILL.md`` YAML front-matter ``description`` must equal
   ``BUILTIN_SKILLS[name]["description"]`` character-for-character. Before
   this PR the registry carried only a COMMENT claiming the two are kept in
   sync ("kept explicit for readability, not because it changes anything")
   with no gate enforcing it -- and in fact they had already drifted (the
   ``draft_judge_revise`` front-matter said '..., a report paragraph). Read
   this...' with a nested double-quote; the registry copy was missing that
   clause and used a single-quote instead). This PR fixes that drift
   (front-matter is the authored source; the registry copy is now
   byte-identical to it) in the same change that adds the gate, per the
   doc-goes-stale-the-moment-the-mechanism-changes rule.

**Sibling sweep (fix-class completeness -- CLAUDE.md "Fix-class sibling
sweep"): why ``BUILTIN_PIPELINES`` gets existence parity but NOT description
parity, and ``BUILTIN_PRESENTATIONS`` gets neither.**

- ``BUILTIN_PIPELINES`` entries DO have the same disk-backed ``path`` shape
  as skills (a YAML file on disk), so existence parity (both directions)
  applies identically and is gated below. Description parity does NOT
  apply: unlike skills (``reyn.data.skills.registry.build_skill_registry``
  reads ``entry["description"]`` directly -- it IS the live, model-facing
  text ``skill_list`` returns), a pipeline's config-entry ``description``
  key is never read by ``reyn.data.pipelines.registry.build_pipeline_registry``
  (grep confirms -- the only "description" reference in that module is a
  docstring sentence). The text the LLM actually sees (IS-5's D19 catalog
  enumerator) is ``Pipeline.description``, parsed FROM the YAML's own
  top-level ``description:`` field by ``reyn.core.pipeline.parser.
  parse_pipeline_dsl`` -- already a single, already-live source of truth
  with no second copy to drift out of sync with the reachable text. The
  ``BUILTIN_PIPELINES["..."]["description"]`` value is inert bookkeeping
  (mirrors the operator config-entry SHAPE per the registry module
  docstring, not a second copy of the SSoT) -- confirmed by observation:
  today it reads differently worded from the YAML's own ``description:``
  field, and forcing verbatim identity there would pin two independently-
  authored blurbs together for no reachability reason.
- ``BUILTIN_PRESENTATIONS`` entries have NO ``path`` key at all -- the
  ``blueprint`` is declared INLINE in the Python dict (see
  ``BUILTIN_PRESENTATIONS["status_card"]`` in registry.py), so there is no
  disk file to check existence against and no front-matter to check
  description against. ``test_no_presentation_gains_a_path_key_silently``
  below is a tripwire: if a future presentation entry ever grows a
  ``path`` key (making it disk-backed like skills/pipelines), this test
  fails LOUDLY instead of the new shape silently evading this gate.

No fakes: imports the REAL ``reyn.builtin.registry`` module and walks the
REAL ``src/reyn/builtin/{skills,pipelines}`` directories on disk.
"""
from __future__ import annotations

from pathlib import Path

import reyn.builtin.registry as registry_module
from reyn.builtin.registry import (
    BUILTIN_PIPELINES,
    BUILTIN_PRESENTATIONS,
    BUILTIN_SKILLS,
)
from reyn.core.frontmatter import split_frontmatter

# Enumeration root derived from the registry module's OWN file location --
# the same way `_BUILTIN_DIR` is computed inside registry.py itself -- so
# this test does not hardcode a path relative to the repo root or to this
# test file (both of which would be a second, driftable copy of "where the
# builtin tree lives").
_BUILTIN_DIR = Path(registry_module.__file__).parent
_SKILLS_DIR = _BUILTIN_DIR / "skills"
_PIPELINES_DIR = _BUILTIN_DIR / "pipelines"


def _skill_md_files_on_disk() -> "set[Path]":
    """Every ``SKILL.md`` directly under ``builtin/skills/<name>/`` --
    NOT ``builtin/plugins/*/skills/`` (plugin skills are a structurally
    different, install-time registration path -- see registry.py's
    ``BUILTIN_SKILLS`` comment on the RAG skill's move under ADR 0064 P5;
    they are never reachable through this always-on registry at all, so
    they are out of scope for a gate about THIS registry)."""
    return {p.resolve() for p in _SKILLS_DIR.glob("*/SKILL.md")}


def _pipeline_yaml_files_on_disk() -> "set[Path]":
    """Every ``*.yaml`` directly under ``builtin/pipelines/`` (same
    plugin-directory exclusion rationale as skills above)."""
    return {p.resolve() for p in _PIPELINES_DIR.glob("*.yaml")}


# ---------------------------------------------------------------------------
# (1) Existence parity -- BUILTIN_SKILLS
# ---------------------------------------------------------------------------


def test_every_builtin_skills_path_exists_on_disk() -> None:
    """Tier 2: OS invariant -- registry -> disk direction. Every
    BUILTIN_SKILLS entry's `path` must resolve to a real file (catches a
    stale/typo'd path pointing at nothing)."""
    assert len(BUILTIN_SKILLS) >= 1, (
        "vacuity guard: BUILTIN_SKILLS is empty -- this gate would pass "
        "trivially with nothing to check"
    )
    for name, entry in BUILTIN_SKILLS.items():
        path = Path(entry["path"])
        assert path.is_file(), (
            f"BUILTIN_SKILLS[{name!r}]['path'] does not exist on disk: {path}"
        )


def test_every_skill_md_on_disk_is_registered_in_builtin_skills() -> None:
    """Tier 2: OS invariant -- disk -> registry direction (the #3163 shape:
    a SKILL.md shipped on disk but never added to BUILTIN_SKILLS is
    permanently undiscoverable via skill_list -- see module docstring)."""
    disk_paths = _skill_md_files_on_disk()
    assert len(disk_paths) >= 1, (
        "vacuity guard: no SKILL.md found under builtin/skills/ -- this "
        "gate would pass trivially with nothing to check"
    )
    registered_paths = {Path(entry["path"]).resolve() for entry in BUILTIN_SKILLS.values()}
    unregistered = disk_paths - registered_paths
    assert not unregistered, (
        "SKILL.md present on disk but missing a BUILTIN_SKILLS entry -- "
        f"permanently undiscoverable via skill_list (#3163): {sorted(unregistered)}"
    )


# ---------------------------------------------------------------------------
# (2) Description verbatim parity -- BUILTIN_SKILLS only
# ---------------------------------------------------------------------------


def test_builtin_skill_descriptions_match_front_matter_verbatim() -> None:
    """Tier 2: OS invariant -- each SKILL.md's YAML front-matter
    `description` must equal BUILTIN_SKILLS[name]["description"]
    character-for-character. Before this PR only a comment in registry.py
    claimed this; no gate enforced it, and the two had already drifted for
    `draft_judge_revise` (fixed in this same PR)."""
    assert len(BUILTIN_SKILLS) >= 1, (
        "vacuity guard: BUILTIN_SKILLS is empty -- this gate would pass "
        "trivially with nothing to check"
    )
    for name, entry in BUILTIN_SKILLS.items():
        path = Path(entry["path"])
        front_matter, _body = split_frontmatter(path.read_text(encoding="utf-8"))
        disk_description = front_matter.get("description")
        assert disk_description == entry["description"], (
            f"BUILTIN_SKILLS[{name!r}]['description'] does not match "
            f"{path}'s front-matter description verbatim:\n"
            f"  registry: {entry['description']!r}\n"
            f"  disk:     {disk_description!r}"
        )


# ---------------------------------------------------------------------------
# (3) Sibling sweep -- BUILTIN_PIPELINES (existence parity only; see module
# docstring for why description parity does not apply)
# ---------------------------------------------------------------------------


def test_every_builtin_pipelines_path_exists_on_disk() -> None:
    """Tier 2: OS invariant -- sibling sweep of the skills existence-parity
    gate above, registry -> disk direction, applied to BUILTIN_PIPELINES
    (same disk-backed `path` shape as skills)."""
    assert len(BUILTIN_PIPELINES) >= 1, (
        "vacuity guard: BUILTIN_PIPELINES is empty -- this gate would pass "
        "trivially with nothing to check"
    )
    for name, entry in BUILTIN_PIPELINES.items():
        path = Path(entry["path"])
        assert path.is_file(), (
            f"BUILTIN_PIPELINES[{name!r}]['path'] does not exist on disk: {path}"
        )


def test_every_pipeline_yaml_on_disk_is_registered_in_builtin_pipelines() -> None:
    """Tier 2: OS invariant -- sibling sweep, disk -> registry direction, for
    BUILTIN_PIPELINES (a pipeline YAML shipped on disk but never registered
    is invisible to the IS-5 catalog enumerator, same unreachability class
    as an unregistered skill)."""
    disk_paths = _pipeline_yaml_files_on_disk()
    assert len(disk_paths) >= 1, (
        "vacuity guard: no *.yaml found under builtin/pipelines/ -- this "
        "gate would pass trivially with nothing to check"
    )
    registered_paths = {Path(entry["path"]).resolve() for entry in BUILTIN_PIPELINES.values()}
    unregistered = disk_paths - registered_paths
    assert not unregistered, (
        "pipeline YAML present on disk but missing a BUILTIN_PIPELINES "
        f"entry -- unreachable via the IS-5 catalog enumerator: {sorted(unregistered)}"
    )


# ---------------------------------------------------------------------------
# (3) Sibling sweep -- BUILTIN_PRESENTATIONS tripwire (no path/disk-body
# shape today; see module docstring for the reasoning)
# ---------------------------------------------------------------------------


def test_no_presentation_gains_a_path_key_silently() -> None:
    """Tier 2: OS invariant -- tripwire, not a parity check. BUILTIN_PRESENTATIONS
    entries declare their `blueprint` inline (no disk file, no `path` key),
    which is why this module does not gate them for existence/description
    parity (see module docstring). If a future presentation entry ever gains
    a `path` key -- becoming disk-backed like skills/pipelines -- this test
    fails LOUDLY so the new shape cannot silently evade this gate instead of
    being added to it."""
    assert len(BUILTIN_PRESENTATIONS) >= 1, (
        "vacuity guard: BUILTIN_PRESENTATIONS is empty -- this gate would "
        "pass trivially with nothing to check"
    )
    path_bearing = {name for name, entry in BUILTIN_PRESENTATIONS.items() if "path" in entry}
    assert not path_bearing, (
        f"BUILTIN_PRESENTATIONS entries now carry a 'path' key: {sorted(path_bearing)} -- "
        "this module's disk-existence/description parity gates must be extended to cover "
        "them (see the sibling-sweep note in this module's docstring)"
    )
