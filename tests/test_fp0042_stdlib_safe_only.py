"""Tier 2 — FP-0042 Phase 3 enforcement: stdlib should not require unsafe-python.

Two enforcement axes:

1. **`reyn.api.unsafe.*` imports in stdlib MUST be zero.** This is the
   strict success criterion from the FP-0042 proposal — stdlib code must
   not reach for raw I/O via the unsafe namespace.

2. **`mode: unsafe` declarations in stdlib skill.md MUST appear in the
   documented exemption set.** A small number of pre-FP-0042 entries are
   grandfathered (= deprecated compat path + skills outside the
   migration's listed Phase 2 scope). The exemption list is mirrored in
   ``docs/concepts/python-safe-mode.md`` under "Stdlib safe-only doctrine
   (FP-0042)" — the test and the doc are kept in lock-step on purpose
   so a CI failure here lands as a documentation update too.

If a new stdlib skill needs unsafe python, the right path is **not** to
add it to the exemption set. Refactor through the `reyn.safe.*` surface
(= file / process / mcp.registry / write_atomic primitives the FP-0042
phases added) or split the I/O out via a ``run_op``. See the FP-0042
proposal at ``docs/deep-dives/proposals/0042-...`` for the migration
patterns the existing 5 skills used.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Documented exemptions — keep in lock-step with python-safe-mode.md
# ---------------------------------------------------------------------------
#
# Each entry: (skill_name, function_name).
# Rationale for each is documented in the safe-mode concept page.
# Adding an entry here is a deliberate decision — pair it with a doc update.

GRANDFATHERED_UNSAFE: set[tuple[str, str]] = set()
# FP-0042 Phase 2.8 (2026-05-23): the last grandfathered exemption,
# ``index_docs.apply_strategy``, was retired. stdlib unsafe surface is
# now zero, and ``GRANDFATHERED_UNSAFE`` stays empty. The Phase 3 test
# below now expresses a stricter invariant: any new stdlib ``mode:
# unsafe`` declaration fails CI — there is no escape hatch.


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _stdlib_root() -> Path:
    """Return the absolute path to src/reyn/stdlib/."""
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "src" / "reyn" / "stdlib"


# ---------------------------------------------------------------------------
# Test A: no reyn.api.unsafe.* imports in stdlib
# ---------------------------------------------------------------------------


_UNSAFE_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+reyn\.api\.unsafe|import\s+reyn\.api\.unsafe)",
    re.MULTILINE,
)


def test_no_unsafe_api_imports_in_stdlib() -> None:
    """Tier 2: no Python file under ``src/reyn/stdlib/`` imports from
    ``reyn.api.unsafe.*``.

    The FP-0042 proposal's headline Phase 3 success criterion. Imports
    are detected by line-anchored regex over the actual python source —
    comment / docstring references that mention the path are not flagged.

    If a new stdlib needs raw I/O, see ``reyn.safe.*`` (= the
    permission-gated surface added by FP-0042) or split the I/O out via
    ``type: run_op``. The proposal at
    ``docs/deep-dives/proposals/0042-stdlib-safe-only-and-permission-gated-file-api.md``
    has the migration patterns the existing 5 skills used.
    """
    violations: list[str] = []
    for py_file in _stdlib_root().rglob("*.py"):
        # Skip __pycache__ / generated files.
        if "__pycache__" in py_file.parts:
            continue
        text = py_file.read_text(encoding="utf-8")
        if _UNSAFE_IMPORT_RE.search(text):
            violations.append(str(py_file.relative_to(_stdlib_root())))

    assert violations == [], (
        f"stdlib python files must not import reyn.api.unsafe.*; "
        f"violations: {violations}. "
        "See FP-0042 (docs/deep-dives/proposals/0042-...) — use "
        "reyn.safe.* instead, or split I/O out via a run_op."
    )


# ---------------------------------------------------------------------------
# Test B: mode: unsafe declarations in stdlib must be in the exemption set
# ---------------------------------------------------------------------------


def _parse_skill_md_frontmatter(skill_md_path: Path) -> dict | None:
    """Extract the YAML frontmatter from a skill.md file.

    Returns the parsed dict, or None if the file has no frontmatter.
    """
    text = skill_md_path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return None
    try:
        return yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None


def _collect_unsafe_python_entries() -> set[tuple[str, str]]:
    """Walk every stdlib skill.md and return the set of (skill, function)
    pairs that declare ``mode: unsafe`` under ``permissions.python``.

    Skill name is taken from the frontmatter ``name`` field (= same as
    the skill identifier used at run time).
    """
    found: set[tuple[str, str]] = set()
    for skill_md in _stdlib_root().rglob("skill.md"):
        fm = _parse_skill_md_frontmatter(skill_md)
        if fm is None:
            continue
        skill_name = str(fm.get("name") or "")
        if not skill_name:
            continue
        perms = fm.get("permissions") or {}
        for entry in perms.get("python") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("mode") == "unsafe":
                fn = str(entry.get("function") or "")
                if fn:
                    found.add((skill_name, fn))
    return found


def test_stdlib_mode_unsafe_only_in_exemption_set() -> None:
    """Tier 2: every ``mode: unsafe`` python step declared in a stdlib
    skill.md must appear in :data:`GRANDFATHERED_UNSAFE`.

    Post-FP-0042 Phase 2.8 (2026-05-23) the exemption set is empty —
    stdlib is fully safe-mode. Adding any new ``mode: unsafe`` entry
    fails this test; the right path is to refactor through
    ``reyn.safe.*`` primitives or split the I/O via a ``run_op``. If a
    new exemption is genuinely required, both this test and
    ``docs/concepts/python-safe-mode.md`` need to be updated together.
    """
    found = _collect_unsafe_python_entries()
    unexpected = found - GRANDFATHERED_UNSAFE
    assert unexpected == set(), (
        f"New stdlib mode: unsafe declaration(s) outside the documented "
        f"exemption set: {sorted(unexpected)}. "
        "Either refactor to mode: safe (= preferred — use reyn.safe.* "
        "primitives or split I/O via run_op), or, if genuinely required, "
        "extend GRANDFATHERED_UNSAFE in this test AND add the entry to "
        "docs/concepts/python-safe-mode.md under the FP-0042 stdlib "
        "safe-only doctrine section."
    )


def test_stdlib_unsafe_surface_is_zero() -> None:
    """Tier 2: stdlib unsafe surface is at the architectural goal of zero.

    Post-FP-0042 Phase 2.8 there are no ``mode: unsafe`` python steps
    in stdlib. This test is the positive form of
    :func:`test_stdlib_mode_unsafe_only_in_exemption_set` — fails fast
    if any unsafe step appears anywhere in stdlib, regardless of the
    exemption set.

    If you must add a stdlib unsafe step, you also need to delete this
    test (= breaking the safe-only doctrine is a deliberate decision
    that needs broad review, not a CI-silent change).
    """
    found = _collect_unsafe_python_entries()
    assert found == set(), (
        f"Stdlib mode: unsafe declarations are not allowed (FP-0042 "
        f"Phase 2.8 closed the last exemption). Found: {sorted(found)}."
    )


def test_grandfathered_exemptions_are_still_present() -> None:
    """Tier 2: stale-exemption guard.

    When a grandfathered entry actually gets migrated to mode: safe, its
    line in :data:`GRANDFATHERED_UNSAFE` should be removed in the same
    PR. This test fails if the exemption set lists a (skill, function)
    that no longer appears in the actual stdlib (= drift between the
    test and reality)."""
    found = _collect_unsafe_python_entries()
    stale = GRANDFATHERED_UNSAFE - found
    assert stale == set(), (
        f"GRANDFATHERED_UNSAFE lists entries that are no longer "
        f"declared as mode: unsafe in stdlib (or no longer exist): "
        f"{sorted(stale)}. Remove the entry from this test and from "
        "docs/concepts/python-safe-mode.md — the migration succeeded, "
        "the carve-out is no longer needed."
    )
