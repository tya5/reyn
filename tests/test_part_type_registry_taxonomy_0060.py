"""Tier 2: proposal 0060 §3 F1 — part-type meta-registry is DERIVED (discovery,
not a hand-list) + taxonomy completeness gate, mirroring the
``OP_KIND_MODEL_MAP`` <-> ``control-ir.md`` sync discipline (CLAUDE.md hard
rule) and ``BUILTIN_HOOK_SCHEMAS``'s registry-walk gate
(``tests/test_hook_event_schema_registry_sync_0059.py``).

``reyn.core.part_type_registry.PART_TYPE_REGISTRY`` is BUILT by walking the
``reyn.core.part_types`` package and collecting every ``PART_TYPE_SPEC``
marker — there is no central list. Three witnesses prove the completeness
properties this design must have:

1. **auto-discovery (the completeness property)**: dropping a NEW marked
   module into the discovered package — touching NOTHING in the collector or
   any central list — makes the new part-type auto-appear in the built map.
   This is the "can't miss a module" proof; a hand-list collector would
   require an edit and so fail this. Witnessed twice: against an isolated
   throwaway package (the generic dir-walk mechanism) AND against the REAL
   default ``reyn.core.part_types`` package (the shipped wiring).
2. **live registry_ref (no dead field)**: a marker whose ``registry_ref``
   points at a nonexistent symbol fails the build RED — the ref is resolved,
   not merely shape-checked.
3. **taxonomy category**: every collected part-type has a non-empty category;
   a marker with an empty category is a violation.

Real modules only, no mocks (testing policy): the throwaway packages are real
on-disk packages imported through the real collector; the default-package
witness drops a real ``.py`` into the shipped package dir and re-runs the real
build.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

import reyn.core.part_types as part_types_pkg
from reyn.core.part_type_registry import (
    PART_ROLES,
    PART_TYPE_REGISTRY,
    PartTypeCollectionError,
    all_part_types,
    build_part_type_registry,
    collect_part_types,
    part_types_for_role,
    resolve_registry_ref,
)

_MARKER_SRC = """\
from reyn.core.part_type_registry import PartTypeSpec

PART_TYPE_SPEC = PartTypeSpec(
    name={name!r},
    roles=frozenset({{"workflow"}}),
    category={category!r},
    registry_ref={registry_ref!r},
    description="a test-injected part-type",
)
"""


def _write_module(pkg_dir: Path, filename: str, body: str) -> None:
    (pkg_dir / filename).write_text(body)


def _make_pkg(tmp_path: Path, pkg_name: str):
    """Create a real importable package under *tmp_path* and return it. The
    caller populates it with modules BEFORE calling ``collect_part_types``."""
    pkg_dir = tmp_path / pkg_name
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text('"""throwaway part-type package."""\n')
    sys.path.insert(0, str(tmp_path))
    importlib.invalidate_caches()
    return importlib.import_module(pkg_name)


def _drop_pkg(pkg_name: str, tmp_path: Path) -> None:
    for mod in [m for m in sys.modules if m == pkg_name or m.startswith(pkg_name + ".")]:
        del sys.modules[mod]
    try:
        sys.path.remove(str(tmp_path))
    except ValueError:
        pass


# ── The real shipped registry ───────────────────────────────────────────────


def test_real_registry_discovers_the_five_wired_part_types():
    """Tier 2: the shipped ``reyn.core.part_types`` package discovers exactly
    the 5 currently-wired part-types through the default build path — a wiring
    snapshot (NOT the completeness gate; completeness is the auto-discovery
    witnesses below). ``task`` is deliberately excluded (§2.1)."""
    assert set(all_part_types()) == {"skill", "pipeline", "mcp", "hook", "presentation"}
    assert "task" not in all_part_types()


def test_real_registry_refs_all_live_resolve():
    """Tier 2: every shipped part-type's ``registry_ref`` resolves to a real
    object — the field is live, not dead documentation."""
    for name, spec in PART_TYPE_REGISTRY.items():
        obj = resolve_registry_ref(spec.registry_ref)
        assert obj is not None, f"{name}: registry_ref {spec.registry_ref!r} resolved to None"


def test_real_registry_all_have_categories_and_valid_roles():
    """Tier 2: taxonomy — every discovered part-type has a non-empty category
    and a non-empty role set within PART_ROLES."""
    for name, spec in PART_TYPE_REGISTRY.items():
        assert spec.category and spec.category.strip(), f"{name}: empty category"
        assert spec.roles, f"{name}: empty role set"
        assert spec.roles <= set(PART_ROLES), f"{name}: role(s) outside {PART_ROLES}"


def test_part_types_for_role_is_registry_derived():
    """Tier 2: ``part_types_for_role`` reflects the discovered markers' own role
    declarations — mcp plays all three roles (§2.1), hook is input-only,
    presentation is output-only."""
    assert set(part_types_for_role("input")) == {"mcp", "hook"}
    assert set(part_types_for_role("output")) == {"mcp", "presentation"}
    assert "mcp" in part_types_for_role("workflow")
    with pytest.raises(ValueError):
        part_types_for_role("not-a-role")


# ── Witness 1: auto-discovery (the completeness property) ───────────────────


def test_dropped_marked_module_auto_appears_generic_mechanism(tmp_path):
    """Tier 2: auto-discovery mechanism — a marked module dropped into a walked
    package is collected with NO edit to the collector; an UNMARKED module in
    the same package is NOT collected. This is the "can't miss a module" proof
    on the generic dir-walk (a hand-list collector could not pick up a module
    it was never told about)."""
    pkg = _make_pkg(tmp_path, "witness_pkg_generic")
    try:
        _write_module(
            tmp_path / "witness_pkg_generic",
            "alpha.py",
            _MARKER_SRC.format(
                name="alpha_part",
                category="alpha_cat",
                registry_ref="reyn.data.skills.registry:build_skill_registry",
            ),
        )
        # A sibling module WITHOUT a marker — must be ignored (not a part-type).
        _write_module(
            tmp_path / "witness_pkg_generic",
            "helper.py",
            '"""a non-part-type helper module."""\nVALUE = 1\n',
        )
        importlib.invalidate_caches()
        collected = collect_part_types(pkg)
        assert "alpha_part" in collected, "marked module was not auto-discovered"
        assert collected["alpha_part"].category == "alpha_cat"
        # The unmarked ``helper`` module contributes no part-type (marker is
        # load-bearing): only the marked module is collected.
        assert set(collected) == {"alpha_part"}
    finally:
        _drop_pkg("witness_pkg_generic", tmp_path)


def test_dropped_marked_module_auto_appears_in_default_registry(tmp_path):
    """Tier 2: auto-discovery on the REAL shipped package — drop a new marker
    ``.py`` into ``reyn/core/part_types/`` itself, change NOTHING else (no
    collector edit, no central list), re-run the DEFAULT build, and the new
    part-type auto-appears. This is the literal "add a 6th part-type, touch
    nothing else, it's picked up" acceptance witness. Cleaned up in finally so
    the shipped package is left untouched."""
    real_dir = Path(part_types_pkg.__file__).parent
    injected = real_dir / "_witness_injected_part.py"
    mod_qual = "reyn.core.part_types._witness_injected_part"
    injected.write_text(
        _MARKER_SRC.format(
            name="witness_injected_part",
            category="witness_cat",
            registry_ref="reyn.data.skills.registry:build_skill_registry",
        )
    )
    try:
        importlib.invalidate_caches()
        rebuilt = build_part_type_registry()
        assert "witness_injected_part" in rebuilt, (
            "a marker dropped into the real part_types package did NOT auto-appear "
            "in the default-built registry — discovery is not derived"
        )
        assert rebuilt["witness_injected_part"].category == "witness_cat"
        # And the 5 real ones are still there (drop-in is additive).
        assert {"skill", "pipeline", "mcp", "hook", "presentation"} <= set(rebuilt)
    finally:
        injected.unlink(missing_ok=True)
        sys.modules.pop(mod_qual, None)
        importlib.invalidate_caches()


# ── Witness 2: live registry_ref (stale ref → RED) ──────────────────────────


def test_stale_registry_ref_fails_the_build(tmp_path):
    """Tier 2: falsify — a marker whose ``registry_ref`` points at a
    nonexistent symbol makes the collector RAISE (the ref is live-resolved,
    not merely shape-checked). Restoring a valid ref would make it pass."""
    pkg = _make_pkg(tmp_path, "witness_pkg_stale")
    try:
        _write_module(
            tmp_path / "witness_pkg_stale",
            "broken.py",
            _MARKER_SRC.format(
                name="broken_part",
                category="c",
                registry_ref="reyn.data.skills.registry:this_symbol_does_not_exist",
            ),
        )
        importlib.invalidate_caches()
        with pytest.raises(PartTypeCollectionError, match="does not resolve"):
            collect_part_types(pkg)
    finally:
        _drop_pkg("witness_pkg_stale", tmp_path)


def test_resolve_registry_ref_rejects_malformed_shape():
    """Tier 2: ``resolve_registry_ref`` rejects a non 'module:qualname' string
    (the shape half of the live-resolution contract)."""
    with pytest.raises(PartTypeCollectionError):
        resolve_registry_ref("no_colon_here")


# ── Witness 3: taxonomy category (empty category → violation) ───────────────


def test_empty_category_marker_is_a_taxonomy_violation(tmp_path):
    """Tier 2: falsify — a discovered marker with an EMPTY category is caught
    as a taxonomy violation. Walks whatever the package yields (registry-
    derived), so an uncatalogued part-type turns the check RED instead of
    silently shipping. The real registry (built above) has no such violation."""
    pkg = _make_pkg(tmp_path, "witness_pkg_nocat")
    try:
        _write_module(
            tmp_path / "witness_pkg_nocat",
            "uncatalogued.py",
            _MARKER_SRC.format(
                name="uncatalogued_part",
                category="",  # <-- the defect
                registry_ref="reyn.data.skills.registry:build_skill_registry",
            ),
        )
        importlib.invalidate_caches()
        collected = collect_part_types(pkg)
        violations = [
            name for name, spec in collected.items() if not spec.category.strip()
        ]
        assert "uncatalogued_part" in violations
        # The real shipped registry has zero such violations.
        assert not [
            n for n, s in PART_TYPE_REGISTRY.items() if not s.category.strip()
        ]
    finally:
        _drop_pkg("witness_pkg_nocat", tmp_path)
