"""Tier 2: OS invariant -- front-matter-declared `references:` for a
multi-file skill (#3162 part 3).

**Motivation.** #3162 part 1 (#3169) fixed the immediate cap overflow. Part 2
(a future PR) will let a skill split its body into an L2 router (`SKILL.md`)
+ L3 `references/*.md`, so a skill that is a genuine single-topic index
(`reyn_cheat_sheet`) can keep growing without either truncating or losing
its index-ness by being split by topic. THIS gate is the guard that must
exist BEFORE any consumer declares `references:` (architect co-vet on
#3162: "逆順にすると、ゲートが無い期間に参照切れを作り込む余地が生まれる").

**Why front-matter, not prose links.** Parsing Markdown prose for reference
links is a semantic operation (notation drift breaks it silently — the same
"ゲート化できない" class as #3164's stale-Status check). Declaring
references in YAML front-matter keeps this a structural check: the
`references:` list is machine-read the same way `description` already is
(`tests/test_builtin_registry_disk_parity.py`).

**Convention gated here** (also documented in
`docs/concepts/tools-integrations/skills.md`):

- `references:` is an OPTIONAL front-matter key -- a skill that doesn't
  declare it stays today's single-`SKILL.md` shape, ungated by this module.
- Every declared entry is a bare filename (e.g. ``hooks-and-events.md``)
  resolved against a ``references/`` subdirectory sibling to ``SKILL.md``.

**Four properties, each gated separately (a RED result names which one
broke -- strip-falsify verified per-property, see PR body):**

1. **Link resolution** -- every declared `references:` entry exists on disk
   under `references/`.
2. **Wheel reachability** -- a body path under a skill's `references/`
   subdirectory is servable through the same wheel-safe
   ``read_builtin_body_bytes`` routing ``test_2913_builtin_body_wheel_reachable.py``
   proved for ``SKILL.md`` itself -- proof that the mechanism generalizes
   to ANY body file under ``skills/<name>/``, not just the top-level file.
3. **Orphan detection** -- the on-disk `references/*.md` file set and the
   declared `references:` set match exactly, in both directions (missing
   declared file, AND undeclared file on disk) -- the same bidirectional-
   parity shape as ``test_builtin_registry_disk_parity.py`` (#3168).
4. **Cap** -- each reference file is strictly under the SAME default
   (model-unresolved) inline read cap as ``SKILL.md`` itself
   (``MAX_CONTROL_IR_RESULT_INLINE_BYTES`` -- imported, not re-declared, so
   this cannot silently desync from ``test_skill_md_default_inline_cap_gate.py``).
   Skipping this is exactly how #3162's original hole (a body silently
   truncated by a model-unresolved read) would reopen one directory level
   down, inside ``references/``.

**Vacuity.** At the time of writing NO shipped skill declares
`references:` -- properties 1/2/3/4 above are checked against a REAL,
zero-sized set and pass trivially. That is intentional (the convention is
declared before any consumer exists, per the architect's explicit ordering
above) and MUST NOT be papered over by asserting `len(...) >= 1` the way
the sibling gates do. Instead, the ``Fixture witnesses`` section below
builds synthetic skills under ``tmp_path`` -- one broken per property -- and
proves the SAME checking function used against real skills actually goes
RED for each break, and GREEN for a fully consistent fixture. The checking
function (`_check_skill_references`) is the one piece of logic shared by
both the real-skill tests and the fixture tests, so a fixture GREEN/RED
result is evidence about the real-skill tests' detection power, not a
disconnected toy.

No fakes: real front-matter parser (`reyn.core.frontmatter.split_frontmatter`),
real files on disk (both the shipped tree and `tmp_path` fixtures), and the
real `read_builtin_body_bytes` wheel-read routing.
"""
from __future__ import annotations

import importlib.resources as importlib_resources
from dataclasses import dataclass, field
from pathlib import Path

import reyn.builtin.docs as builtin_docs_module
import reyn.builtin.registry as registry_module
from reyn.builtin.docs import read_builtin_body_bytes
from reyn.builtin.registry import BUILTIN_SKILLS
from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES
from reyn.core.frontmatter import split_frontmatter

_BUILTIN_DIR = Path(registry_module.__file__).parent
_PLUGINS_DIR = _BUILTIN_DIR / "plugins"


# ---------------------------------------------------------------------------
# Enumeration -- same "registry + real directory walk, no hardcoded name
# list" pattern as test_skill_md_default_inline_cap_gate.py, so a newly
# shipped skill (either surface) is picked up automatically.
# ---------------------------------------------------------------------------


def _builtin_registry_skill_paths() -> "set[Path]":
    return {Path(entry["path"]).resolve() for entry in BUILTIN_SKILLS.values()}


def _plugin_skill_md_paths_on_disk() -> "set[Path]":
    if not _PLUGINS_DIR.is_dir():
        return set()
    return {p.resolve() for p in _PLUGINS_DIR.glob("*/skills/*/SKILL.md")}


def _all_shipped_skill_md_paths() -> "set[Path]":
    return _builtin_registry_skill_paths() | _plugin_skill_md_paths_on_disk()


# ---------------------------------------------------------------------------
# Shared checking function -- applied identically to real shipped skills and
# to tmp_path fixtures below (this identity is what makes the fixture
# witnesses evidence about the real-skill gates' detection power).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ReferenceCheckResult:
    skill_md_path: Path
    declared: "list[str]" = field(default_factory=list)
    missing: "list[str]" = field(default_factory=list)  # declared, absent on disk
    orphans: "list[str]" = field(default_factory=list)  # on disk, not declared
    oversize: "dict[str, int]" = field(default_factory=dict)  # declared, >= cap


def _check_skill_references(skill_md_path: Path, cap: int) -> _ReferenceCheckResult:
    """Property (1) link resolution, (3) orphan parity, (4) cap -- for one
    skill's `references:` front-matter declaration against its `references/`
    directory. Property (2) wheel reachability is checked separately below
    (it needs the real `reyn.builtin` package resolution, not a plain stat)."""
    references_dir = skill_md_path.parent / "references"
    front_matter, _body = split_frontmatter(skill_md_path.read_text(encoding="utf-8"))
    declared = list(front_matter.get("references") or [])

    missing: "list[str]" = []
    oversize: "dict[str, int]" = {}
    for name in declared:
        ref_path = references_dir / name
        if not ref_path.is_file():
            missing.append(name)
            continue
        size = len(ref_path.read_text(encoding="utf-8"))
        if size >= cap:
            oversize[name] = size

    disk_names = {p.name for p in references_dir.glob("*.md")} if references_dir.is_dir() else set()
    orphans = sorted(disk_names - set(declared))

    return _ReferenceCheckResult(
        skill_md_path=skill_md_path,
        declared=declared,
        missing=sorted(missing),
        orphans=orphans,
        oversize=oversize,
    )


# ---------------------------------------------------------------------------
# Real-skill gates (1) link resolution, (3) orphan parity, (4) cap.
# Vacuous today (0 skills declare `references:`) -- see module docstring and
# the fixture witnesses below for why that is acceptable here.
# ---------------------------------------------------------------------------


def test_every_declared_reference_resolves_and_has_no_orphans_and_fits_cap() -> None:
    """Tier 2: OS invariant -- properties (1)+(3)+(4) against every shipped
    skill. Vacuous today (see module docstring): 0 shipped skills currently
    declare `references:`, so this passes trivially. Detection power is
    proven by the fixture witnesses below, which apply the SAME
    `_check_skill_references` function to synthetic broken skills."""
    all_paths = _all_shipped_skill_md_paths()
    assert len(all_paths) >= 1, (
        "vacuity guard: no SKILL.md found across either shipping surface -- "
        "this gate would pass trivially with nothing to check"
    )

    missing_by_skill = {}
    orphans_by_skill = {}
    oversize_by_skill = {}
    for path in sorted(all_paths):
        result = _check_skill_references(path, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
        if result.missing:
            missing_by_skill[str(path)] = result.missing
        if result.orphans:
            orphans_by_skill[str(path)] = result.orphans
        if result.oversize:
            oversize_by_skill[str(path)] = result.oversize

    assert not missing_by_skill, (
        "declared `references:` entry does not exist under references/: "
        f"{missing_by_skill}"
    )
    assert not orphans_by_skill, (
        "file(s) under references/ are not declared in front-matter "
        f"`references:` (orphan, unreachable via the router): {orphans_by_skill}"
    )
    assert not oversize_by_skill, (
        "reference file exceeds the default (model-unresolved) inline read "
        f"cap ({MAX_CONTROL_IR_RESULT_INLINE_BYTES} chars): {oversize_by_skill}"
    )


# ---------------------------------------------------------------------------
# Real-skill gate (2) wheel reachability -- generalizes test_2913's proof
# (SKILL.md is readable via read_builtin_body_bytes even when project_root
# is elsewhere) to whatever `references:` a skill declares. Vacuous today
# for the same reason as above; the fixture witness proves the underlying
# routing generalizes beyond the top-level SKILL.md filename.
# ---------------------------------------------------------------------------


def test_every_declared_reference_is_wheel_reachable() -> None:
    """Tier 2: OS invariant -- every declared reference, for every shipped
    skill, is servable through read_builtin_body_bytes (the same wheel-safe
    routing test_2913 proved for SKILL.md). Vacuous today (0 declared
    references) -- see the fixture-based generalization witness below."""
    all_paths = _all_shipped_skill_md_paths()
    unreachable = {}
    for path in sorted(all_paths):
        result = _check_skill_references(path, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
        for name in result.declared:
            ref_path = path.parent / "references" / name
            if not ref_path.is_file():
                continue  # already reported by the link-resolution gate above
            if read_builtin_body_bytes(str(ref_path)) is None:
                unreachable.setdefault(str(path), []).append(name)
    assert not unreachable, (
        f"declared reference(s) not reachable via read_builtin_body_bytes: {unreachable}"
    )


# ---------------------------------------------------------------------------
# Fixture witnesses -- vacuity guard. Each test builds a synthetic skill
# under tmp_path with exactly one property broken and asserts the SHARED
# checking function (used by the real-skill gates above) goes RED for it.
# A final control fixture is fully consistent and must go GREEN across all
# four properties, proving the checks are not vacuously always-red either.
# ---------------------------------------------------------------------------


def _write_skill(skill_dir: Path, *, references: "list[str] | None", body_extra: str = "") -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    front_matter_lines = ["---", "name: fixture_skill", "description: witness fixture"]
    if references is not None:
        front_matter_lines.append("references:")
        front_matter_lines.extend(f"  - {name}" for name in references)
    front_matter_lines.append("---")
    text = "\n".join(front_matter_lines) + f"\n\n# Fixture\n\n{body_extra}"
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(text, encoding="utf-8")
    return skill_md


def test_witness_missing_declared_reference_is_detected(tmp_path) -> None:
    """Tier 2: fixture witness (a) -- a `references:` entry declared in
    front-matter but absent on disk under references/ must be flagged as
    `missing` by `_check_skill_references` (property 1, link resolution)."""
    skill_md = _write_skill(tmp_path / "fixture_skill", references=["does-not-exist.md"])
    result = _check_skill_references(skill_md, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
    assert result.missing == ["does-not-exist.md"], result
    assert not result.orphans
    assert not result.oversize


def test_witness_orphan_reference_file_is_detected(tmp_path) -> None:
    """Tier 2: fixture witness (b) -- a file sitting under references/ that
    is NOT declared in front-matter `references:` must be flagged as an
    `orphan` by `_check_skill_references` (property 3, bidirectional parity)."""
    skill_dir = tmp_path / "fixture_skill"
    skill_md = _write_skill(skill_dir, references=[])
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "undeclared.md").write_text("orphan content", encoding="utf-8")
    result = _check_skill_references(skill_md, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
    assert result.orphans == ["undeclared.md"], result
    assert not result.missing
    assert not result.oversize


def test_witness_oversize_reference_is_detected(tmp_path) -> None:
    """Tier 2: fixture witness (c) -- a declared reference file at/over the
    default inline cap must be flagged as `oversize` by
    `_check_skill_references` (property 4, cap)."""
    skill_dir = tmp_path / "fixture_skill"
    skill_md = _write_skill(skill_dir, references=["huge.md"])
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "huge.md").write_text(
        "x" * MAX_CONTROL_IR_RESULT_INLINE_BYTES, encoding="utf-8"
    )
    result = _check_skill_references(skill_md, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
    assert result.oversize == {"huge.md": MAX_CONTROL_IR_RESULT_INLINE_BYTES}, result
    assert not result.missing
    assert not result.orphans


def test_witness_consistent_fixture_is_fully_green(tmp_path) -> None:
    """Tier 2: fixture control -- a skill whose `references:` declaration
    exactly matches its references/ directory, all under cap, must report
    NO missing/orphan/oversize entries. Without this control, a checker
    that always reports something broken would pass the three RED witnesses
    above vacuously."""
    skill_dir = tmp_path / "fixture_skill"
    skill_md = _write_skill(skill_dir, references=["a.md", "b.md"])
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "a.md").write_text("small a", encoding="utf-8")
    (references_dir / "b.md").write_text("small b", encoding="utf-8")
    result = _check_skill_references(skill_md, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
    assert not result.missing
    assert not result.orphans
    assert not result.oversize


def test_witness_no_references_declared_is_ungated(tmp_path) -> None:
    """Tier 2: fixture control -- a skill with no `references:` key at all
    (today's shape for every shipped skill) reports nothing broken --
    `references:` is opt-in, not a new requirement on existing skills."""
    skill_md = _write_skill(tmp_path / "fixture_skill", references=None)
    result = _check_skill_references(skill_md, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
    assert result.declared == []
    assert not result.missing
    assert not result.orphans
    assert not result.oversize


def test_witness_wheel_reachability_generalizes_beyond_skill_md(tmp_path, monkeypatch) -> None:
    """Tier 2: fixture witness for property (2) -- proves
    `read_builtin_body_bytes` reaches a body path under `skills/<name>/`
    generically (not special-cased to the literal SKILL.md filename), by
    redirecting the function's package-root resolution to a synthetic
    `skills/<name>/references/*.md` tree under tmp_path. Falsify: a file
    OUTSIDE the body dirs (skills/pipelines) still returns None, proving
    the least-privilege scope (#2914 Ruling 1) is not widened by this
    generalization."""
    package_root = tmp_path / "package_root"
    skill_dir = package_root / "skills" / "fixture_skill"
    references_dir = skill_dir / "references"
    references_dir.mkdir(parents=True)
    ref_path = references_dir / "hooks-and-events.md"
    ref_path.write_text("reference body content", encoding="utf-8")

    non_body_path = package_root / "registry.py"
    non_body_path.write_text("not a body file", encoding="utf-8")

    monkeypatch.setattr(importlib_resources, "files", lambda pkg: package_root)
    # docs.py imports `importlib.resources as _resources` at module scope --
    # patch that bound name directly so the real function under test picks
    # up the redirected resolver.
    monkeypatch.setattr(builtin_docs_module._resources, "files", lambda pkg: package_root)

    reachable = read_builtin_body_bytes(str(ref_path))
    assert reachable == b"reference body content", reachable

    unreachable = read_builtin_body_bytes(str(non_body_path))
    assert unreachable is None, (
        "a path inside the package but outside skills/ or pipelines/ must "
        "stay gated -- the reference generalization must not widen scope"
    )
