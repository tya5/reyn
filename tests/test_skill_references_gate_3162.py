"""Tier 2: OS invariant -- standard `${CLAUDE_SKILL_DIR}`/`${REYN_SKILL_DIR}`
Markdown-link references from a `SKILL.md` body to a bundled `references/`
file (#3162 part 4, rebuild).

**Why this replaced the front-matter `references:` gate.** #3170 introduced
a NON-standard front-matter `references:` front-matter key as the mechanism
for declaring a skill's bundled files. The official Anthropic Agent Skills
spec has no such field -- the standard way to reference a bundled file from
`SKILL.md` is a plain Markdown link in the body
(https://code.claude.com/docs/en/skills.md). This module removes the
front-matter convention entirely and gates the standard mechanism instead:
a Markdown link whose URL is `${CLAUDE_SKILL_DIR}/<path>` (or reyn's
underlying `${REYN_SKILL_DIR}` -- `src/reyn/plugins/tokens.py` aliases the
former to the latter). A bare relative path written in prose (e.g.
``references/foo.md``) is NOT reachable at read time: reyn's `file` read op
resolves a non-absolute path against the WORKSPACE root, not the skill's own
directory (`src/reyn/core/op_runtime/file.py`) -- only the token-prefixed
absolute-path form resolves correctly, and only `SKILL.md` itself gets
token expansion (`src/reyn/plugins/skill_load.py`, matched on basename).

**Four properties, each gated separately** (a RED result names which one
broke -- strip-falsify verified per-property, see PR body):

1. **Link resolution** -- every `${CLAUDE_SKILL_DIR}`/`${REYN_SKILL_DIR}`-
   prefixed Markdown link in a `SKILL.md` body points at a file that exists
   under that skill's own directory.
2. **Bidirectional orphan parity** -- for a skill with a `references/`
   subdirectory, the file set actually present under `references/` and the
   set of property-1 link targets pointing into `references/` match exactly
   in both directions (no file on disk undeclared by any link, no link
   dangling at a missing file).
3. **Size cap** -- every `.md` file anywhere under a skill's directory
   (including `SKILL.md` itself) stays strictly under
   `MAX_CONTROL_IR_RESULT_INLINE_BYTES` -- the same model-unresolved
   inline-read floor `test_skill_md_default_inline_cap_gate.py` gates for
   `SKILL.md` alone, generalized to every shipped `.md` body.
4. **L3-is-a-leaf** -- a file under a skill's `references/` directory must
   NOT itself contain a token-prefixed Markdown link to another file. Only
   `SKILL.md` gets invocation-time token expansion (point 2 above), so a
   link like this sitting inside a reference file would never resolve for
   the model reading it -- it is always a bug, not a valid second level of
   the progressive-disclosure chain (L1 menu -> L2 router `SKILL.md` -> L3
   reference, one level deep by design). NOTE: a bare `${...}` token used
   in prose or a command example inside a reference file is NOT itself a
   violation -- only a `](${TOKEN}/...)` -shaped Markdown link is in scope
   (a peer-session correction folded into this rebuild after the token-ban
   framing of an earlier draft proved too broad: `tokens.py`'s own docs
   treat an unexpanded token as an ordinary, expected outcome, and banning
   all token mentions would leave a reference file no way to legitimately
   talk about `${CLAUDE_SKILL_DIR}` at all).

**Enumeration -- real directory walk + registry, no hardcoded name list**
(same pattern as `test_skill_md_default_inline_cap_gate.py` /
`test_builtin_registry_disk_parity.py`, so a newly shipped skill on either
surface is picked up automatically): `BUILTIN_SKILLS`' registered `SKILL.md`
paths, unioned with every `SKILL.md` found by walking
``src/reyn/builtin/plugins/*/skills/*/``.

**Vacuity guards.** The real-tree test asserts it found a non-zero number of
skills AND (separately) that it found a non-zero number of token-prefixed
links across those skills -- reyn_cheat_sheet already ships 4 such links
(#3162 fix), so an empty match set here would indicate the extraction regex
itself broke, not that no skill uses the mechanism.

No fakes: real front-matter/body split via `split()`-on-Markdown (no YAML
front-matter is consulted by this gate at all -- it is a pure Markdown-link
scan), real files on disk (both the shipped tree and `tmp_path` fixtures).
Each of the 4 properties has a dedicated fixture built under `tmp_path` that
breaks ONLY that property and is asserted to go RED via the SAME shared
checking function the real-tree test uses, plus a fully-consistent control
fixture proving the checks are not vacuously always-red.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import reyn.builtin.registry as registry_module
from reyn.builtin.registry import BUILTIN_SKILLS
from reyn.core.context_builder import MAX_CONTROL_IR_RESULT_INLINE_BYTES

_BUILTIN_DIR = Path(registry_module.__file__).parent
_PLUGINS_DIR = _BUILTIN_DIR / "plugins"

# Matches a Markdown link `[text](${CLAUDE_SKILL_DIR}/path/to/file.md)` or
# the `${REYN_SKILL_DIR}` alias it expands to (`tokens.py`). Captures the
# path suffix after the token. Deliberately a literal string match on the
# token, NOT an attempt to expand it -- expansion is skill_load.py's job,
# this gate only checks the reachable *form*.
_TOKEN_LINK_RE = re.compile(
    r"\]\(\$\{(?:CLAUDE_SKILL_DIR|REYN_SKILL_DIR)\}/([^)\s]+)\)"
)


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
    links: "list[str]" = field(default_factory=list)  # extracted link targets
    missing: "list[str]" = field(default_factory=list)  # link target absent on disk
    orphans: "list[str]" = field(default_factory=list)  # under references/, no link
    dangling: "list[str]" = field(default_factory=list)  # linked into references/, absent
    oversize: "dict[str, int]" = field(default_factory=dict)  # .md file >= cap
    nested_links: "dict[str, list[str]]" = field(default_factory=dict)  # ref file -> its own links


def extract_token_links(text: str) -> "list[str]":
    """Property (1)'s extraction primitive: every ``${CLAUDE_SKILL_DIR}``/
    ``${REYN_SKILL_DIR}``-prefixed Markdown link target found in *text*, as
    the raw path suffix after the token (e.g. ``references/foo.md``)."""
    return _TOKEN_LINK_RE.findall(text)


def _check_skill(skill_md_path: Path, cap: int) -> _ReferenceCheckResult:
    """Properties (1) link resolution, (2) bidirectional orphan parity,
    (3) size cap, (4) L3-is-a-leaf -- for one skill directory."""
    skill_dir = skill_md_path.parent
    body = skill_md_path.read_text(encoding="utf-8")
    links = extract_token_links(body)

    missing: "list[str]" = []
    for rel in links:
        if not (skill_dir / rel).is_file():
            missing.append(rel)

    references_dir = skill_dir / "references"
    orphans: "list[str]" = []
    dangling: "list[str]" = []
    if references_dir.is_dir():
        disk_files = {
            str(p.relative_to(skill_dir)) for p in references_dir.rglob("*") if p.is_file()
        }
        linked_into_references = {
            rel for rel in links if _is_under(skill_dir / rel, references_dir)
        }
        orphans = sorted(disk_files - linked_into_references)
        dangling = sorted(
            rel for rel in linked_into_references if not (skill_dir / rel).is_file()
        )

    oversize: "dict[str, int]" = {}
    for md_path in skill_dir.rglob("*.md"):
        size = len(md_path.read_text(encoding="utf-8"))
        if size >= cap:
            oversize[str(md_path.relative_to(skill_dir))] = size

    nested_links: "dict[str, list[str]]" = {}
    if references_dir.is_dir():
        for ref_path in references_dir.rglob("*.md"):
            found = extract_token_links(ref_path.read_text(encoding="utf-8"))
            if found:
                nested_links[str(ref_path.relative_to(skill_dir))] = found

    return _ReferenceCheckResult(
        skill_md_path=skill_md_path,
        links=links,
        missing=sorted(missing),
        orphans=orphans,
        dangling=dangling,
        oversize=oversize,
        nested_links=nested_links,
    )


def _is_under(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Real-skill gate -- all 4 properties, against every shipped skill.
# ---------------------------------------------------------------------------


def test_every_skill_reference_link_resolves_has_no_orphans_fits_cap_and_leaf() -> None:
    """Tier 2: OS invariant -- properties (1)+(2)+(3)+(4) against every
    shipped skill (builtin registry + plugin skills-on-disk). Vacuity-
    guarded: asserts a non-zero skill count AND a non-zero token-link count
    were actually found, so an empty match set cannot silently pass."""
    all_paths = _all_shipped_skill_md_paths()
    assert len(all_paths) >= 1, (
        "vacuity guard: no SKILL.md found across either shipping surface -- "
        "this gate would pass trivially with nothing to check"
    )

    missing_by_skill: "dict[str, list[str]]" = {}
    orphans_by_skill: "dict[str, list[str]]" = {}
    dangling_by_skill: "dict[str, list[str]]" = {}
    oversize_by_skill: "dict[str, dict[str, int]]" = {}
    nested_by_skill: "dict[str, dict[str, list[str]]]" = {}
    total_links = 0

    for path in sorted(all_paths):
        result = _check_skill(path, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
        total_links += len(result.links)
        if result.missing:
            missing_by_skill[str(path)] = result.missing
        if result.orphans:
            orphans_by_skill[str(path)] = result.orphans
        if result.dangling:
            dangling_by_skill[str(path)] = result.dangling
        if result.oversize:
            oversize_by_skill[str(path)] = result.oversize
        if result.nested_links:
            nested_by_skill[str(path)] = result.nested_links

    assert total_links >= 1, (
        "vacuity guard: no ${CLAUDE_SKILL_DIR}/${REYN_SKILL_DIR}-prefixed "
        "link found across any shipped skill -- reyn_cheat_sheet ships 4 "
        "such links, so an empty count here means the extraction regex "
        "itself broke, not that the mechanism is unused"
    )

    assert not missing_by_skill, (
        "a ${CLAUDE_SKILL_DIR}/${REYN_SKILL_DIR}-prefixed link's target does "
        f"not exist under the skill's own directory: {missing_by_skill}"
    )
    assert not orphans_by_skill, (
        "file(s) under references/ are not linked from SKILL.md via a "
        f"token-prefixed link (unreachable via the router): {orphans_by_skill}"
    )
    assert not dangling_by_skill, (
        "a token-prefixed link points into references/ at a file that does "
        f"not exist: {dangling_by_skill}"
    )
    assert not oversize_by_skill, (
        "a .md file under a skill directory exceeds the default "
        f"(model-unresolved) inline read cap ({MAX_CONTROL_IR_RESULT_INLINE_BYTES} "
        f"chars): {oversize_by_skill}"
    )
    assert not nested_by_skill, (
        "a file under references/ itself contains a token-prefixed link to "
        "another file -- L3 references must be leaves (only SKILL.md gets "
        f"token expansion): {nested_by_skill}"
    )


# ---------------------------------------------------------------------------
# Fixture witnesses -- vacuity guard. Each test builds a synthetic skill
# under tmp_path with exactly one property broken and asserts the SHARED
# checking function (used by the real-skill gate above) goes RED for it,
# while staying silent about the other 3 properties. A final control
# fixture is fully consistent and must go GREEN across all four properties.
# ---------------------------------------------------------------------------


def _write_skill(skill_dir: Path, *, body_links: str = "") -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        "name: fixture_skill\n"
        "description: witness fixture\n"
        "---\n\n"
        "# Fixture\n\n"
        f"{body_links}\n"
    )
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(text, encoding="utf-8")
    return skill_md


def test_witness_dangling_reference_link_is_detected(tmp_path) -> None:
    """Tier 2: fixture witness (a) -- a ${CLAUDE_SKILL_DIR}-prefixed link in
    SKILL.md whose target does not exist on disk must be flagged as
    `missing` (property 1, link resolution) -- and must NOT trip
    orphans/oversize/nested_links."""
    skill_dir = tmp_path / "fixture_skill"
    skill_md = _write_skill(
        skill_dir,
        body_links="[gone](${CLAUDE_SKILL_DIR}/references/does-not-exist.md)",
    )
    result = _check_skill(skill_md, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
    assert result.missing == ["references/does-not-exist.md"], result
    assert not result.orphans
    assert not result.dangling
    assert not result.oversize
    assert not result.nested_links


def test_witness_orphan_reference_file_is_detected(tmp_path) -> None:
    """Tier 2: fixture witness (b) -- a file sitting under references/ that
    NO token-prefixed link in SKILL.md points at must be flagged as an
    `orphan` (property 2, bidirectional parity) -- and must NOT trip
    missing/oversize/nested_links."""
    skill_dir = tmp_path / "fixture_skill"
    skill_md = _write_skill(skill_dir, body_links="No links here.")
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "undeclared.md").write_text("orphan content", encoding="utf-8")
    result = _check_skill(skill_md, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
    assert result.orphans == ["references/undeclared.md"], result
    assert not result.missing
    assert not result.dangling
    assert not result.oversize
    assert not result.nested_links


def test_witness_oversize_md_file_is_detected(tmp_path) -> None:
    """Tier 2: fixture witness (c) -- an .md file (SKILL.md or a reference)
    at/over the default inline cap must be flagged as `oversize`
    (property 3, size cap) -- and must NOT trip missing/orphans/nested_links
    (the reference is correctly linked, just too big)."""
    skill_dir = tmp_path / "fixture_skill"
    skill_md = _write_skill(
        skill_dir,
        body_links="[huge](${CLAUDE_SKILL_DIR}/references/huge.md)",
    )
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "huge.md").write_text(
        "x" * MAX_CONTROL_IR_RESULT_INLINE_BYTES, encoding="utf-8"
    )
    result = _check_skill(skill_md, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
    assert result.oversize == {
        "references/huge.md": MAX_CONTROL_IR_RESULT_INLINE_BYTES
    }, result
    assert not result.missing
    assert not result.orphans
    assert not result.dangling
    assert not result.nested_links


def test_witness_nested_reference_link_is_detected(tmp_path) -> None:
    """Tier 2: fixture witness (d) -- a reference file that itself contains
    a token-prefixed Markdown link to another file must be flagged in
    `nested_links` (property 4, L3-is-a-leaf) -- and must NOT trip
    missing/orphans/oversize. A bare ${...} mention that is NOT inside a
    `](${TOKEN}/...)` link shape must NOT trip this check (the peer
    correction folded into this rebuild: token mentions in prose are fine,
    only the link *form* is gated)."""
    skill_dir = tmp_path / "fixture_skill"
    skill_md = _write_skill(
        skill_dir,
        body_links="[ref](${CLAUDE_SKILL_DIR}/references/leaf.md)",
    )
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "leaf.md").write_text(
        "See ${CLAUDE_SKILL_DIR} in prose (fine on its own), but also a real "
        "nested link: [deeper](${CLAUDE_SKILL_DIR}/references/other.md)",
        encoding="utf-8",
    )
    (references_dir / "other.md").write_text("unrelated leaf", encoding="utf-8")
    result = _check_skill(skill_md, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
    assert result.nested_links == {
        "references/leaf.md": ["references/other.md"]
    }, result
    assert not result.missing
    assert not result.oversize
    # other.md is on disk but not linked from SKILL.md itself (only from the
    # nested, invalid link inside leaf.md) -- it correctly still reports as
    # an orphan too, since property (2) only counts links FROM SKILL.md.
    assert result.orphans == ["references/other.md"]


def test_witness_consistent_fixture_is_fully_green(tmp_path) -> None:
    """Tier 2: fixture control -- a skill whose SKILL.md links match its
    references/ directory exactly, all under cap, with no nested links,
    must report NO missing/orphan/dangling/oversize/nested entries. Without
    this control, a checker that always reports something broken would pass
    the four RED witnesses above vacuously."""
    skill_dir = tmp_path / "fixture_skill"
    skill_md = _write_skill(
        skill_dir,
        body_links=(
            "[a](${CLAUDE_SKILL_DIR}/references/a.md)\n"
            "[b](${REYN_SKILL_DIR}/references/b.md)\n"
        ),
    )
    references_dir = skill_dir / "references"
    references_dir.mkdir()
    (references_dir / "a.md").write_text("small a", encoding="utf-8")
    (references_dir / "b.md").write_text("small b", encoding="utf-8")
    result = _check_skill(skill_md, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
    assert not result.missing
    assert not result.orphans
    assert not result.dangling
    assert not result.oversize
    assert not result.nested_links


def test_witness_no_references_dir_is_ungated(tmp_path) -> None:
    """Tier 2: fixture control -- a skill with no references/ directory at
    all (a genuinely single-file skill) reports nothing broken --
    references/ is opt-in, not a new requirement on existing skills."""
    skill_md = _write_skill(tmp_path / "fixture_skill", body_links="No references needed.")
    result = _check_skill(skill_md, MAX_CONTROL_IR_RESULT_INLINE_BYTES)
    assert result.links == []
    assert not result.missing
    assert not result.orphans
    assert not result.dangling
    assert not result.oversize
    assert not result.nested_links
