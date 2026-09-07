"""Tier 2: #5058 — the core-dependency ``importorskip`` gate.

``pytest.importorskip("x")`` declares "x is optional, skip if absent" —
correct for an ``extra``, wrong for a ``pyproject.toml`` ``[project].
dependencies`` entry: a broken install there should fail collection
loudly, not skip green (CLAUDE.md's six-questions Q4). 17
``fastapi``/``starlette``/``uvicorn``/``websockets`` files (#5051) and 12
``mcp`` files were found and fixed by hand across this issue's own
thread — this gate closes the CLASS so a future core-dep
``importorskip`` (a new file, or a reintroduced one) is caught
automatically.

Real filesystem fixtures throughout (a real ``tmp_path`` tree of ``.py``
files and a real synthetic ``pyproject.toml``) — the functions under
test read real file content and parse a real TOML document, so faking
either would test nothing real.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_no_core_dep_importorskip import (
    _ROOT,
    _TESTS_DIR,
    IMPORT_NAME_MANIFEST,
    core_dependency_names,
    core_import_names,
    manifest_gaps,
    offending_calls,
)

_SYNTHETIC_PYPROJECT = """\
[project]
name = "synthetic"
dependencies = [
    "reallycore>=1.0",
    "pillow>=11.0",
    "uvicorn[standard]>=0.27",
    "textual-flowview @ git+https://example.invalid/x.git@deadbeef",
]
"""

# A pyproject.toml naming a REAL, already-IMPORT_NAME_MANIFEST-covered core
# dependency ("mcp") -- used by the actual gate (offending_calls) tests below,
# which need a name the manifest already maps, unlike _SYNTHETIC_PYPROJECT's
# "reallycore" (deliberately unmapped, for the name-extraction/gap tests
# above).
_SYNTHETIC_PYPROJECT_WITH_MCP = '[project]\ndependencies = ["mcp>=2.0"]\n'


def _write_pyproject(tmp_path: Path, content: str = _SYNTHETIC_PYPROJECT) -> Path:
    p = tmp_path / "pyproject.toml"
    p.write_text(content, encoding="utf-8")
    return p


# ── core_dependency_names: the single source of truth, parsed not guessed ──


def test_core_dependency_names_strips_extras_versions_and_direct_urls(
    tmp_path: Path,
) -> None:
    """Tier 2: the PEP 508 shapes actually present in reyn's own
    pyproject.toml today — a version specifier, an ``[extras]`` bracket
    (``uvicorn[standard]``), and a direct ``@ git+`` URL
    (``textual-flowview``) — all resolve to their bare distribution name,
    never a hardcoded list."""
    names = core_dependency_names(_write_pyproject(tmp_path))
    assert names == ["reallycore", "pillow", "uvicorn", "textual-flowview"]


# ── manifest completeness — the "increased -> decide" requirement ──────────


def test_manifest_has_no_gaps_for_the_real_repo() -> None:
    """Tier 2: the REAL repo's pyproject.toml — every current core
    dependency has an IMPORT_NAME_MANIFEST entry. A gap here means a NEW
    core dependency was declared and this manifest was not updated —
    architect's "increased -> decide" requirement, checked against the
    live tree, not assumed."""
    real_deps = core_dependency_names(_ROOT / "pyproject.toml")
    gaps = manifest_gaps(real_deps)
    assert gaps == [], (
        f"pyproject.toml declares core dependency name(s) "
        f"{gaps} with no IMPORT_NAME_MANIFEST entry in "
        "scripts/check_no_core_dep_importorskip.py -- add its import "
        "name(s) there"
    )


def test_a_core_dep_missing_from_the_manifest_is_a_distinct_failure(
    tmp_path: Path,
) -> None:
    """Tier 2: a synthetic pyproject.toml naming a dependency the manifest
    has never heard of is reported as a GAP, not silently treated as
    non-core (which would let a future mismatched name — the next
    ``pillow``/``PIL`` — pass this gate unnoticed)."""
    pyproject = _write_pyproject(
        tmp_path, '[project]\ndependencies = ["totally-unmapped-package>=1.0"]\n'
    )
    assert "totally-unmapped-package" not in IMPORT_NAME_MANIFEST
    gaps = manifest_gaps(core_dependency_names(pyproject))
    assert gaps == ["totally-unmapped-package"]


# ── the name-mismatch trap architect named ──────────────────────────────────


def test_name_mismatch_is_caught_via_the_manifest_not_naive_derivation(
    tmp_path: Path,
) -> None:
    """Tier 2: ``pillow`` (distribution name) imports as ``PIL`` — a naive
    lowercase-hyphen-to-underscore derivation would produce ``pillow``,
    missing an actual ``importorskip("PIL")`` entirely. The manifest's
    explicit entry is what makes this catchable."""
    pyproject = _write_pyproject(tmp_path)
    names = core_import_names(core_dependency_names(pyproject))
    assert "PIL" in names
    assert "pillow" not in names  # the WRONG (naive) derivation must not appear


# ── the actual gate: importorskip naming a core dependency ─────────────────


def _make_tests_tree(tmp_path: Path, filename: str, body: str) -> Path:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / filename).write_text(body, encoding="utf-8")
    return tests_dir


def test_a_core_dep_importorskip_is_flagged(tmp_path: Path) -> None:
    """Tier 2: THE strip witness (#5058's own acceptance criterion) — a
    file added to the tests tree with ``pytest.importorskip("mcp", ...)``
    (a REAL, manifest-covered core dependency) is flagged."""
    pyproject = _write_pyproject(tmp_path, _SYNTHETIC_PYPROJECT_WITH_MCP)
    tests_dir = _make_tests_tree(
        tmp_path, "test_a.py",
        'import pytest\npytest.importorskip("mcp", reason="x")\n',
    )
    offenders = offending_calls(tests_dir, pyproject)
    assert offenders == [(tests_dir / "test_a.py", 2, "mcp")]


def test_a_core_dep_importorskip_via_dotted_submodule_is_flagged(
    tmp_path: Path,
) -> None:
    """Tier 2: ``importorskip("mcp.sub")`` names a SUBMODULE of a core
    dependency — flagged via its top-level component, the same
    ``"mcp.server"`` -> ``"mcp"`` shape architect's own real-world census
    found (#5058 thread, comment 8)."""
    pyproject = _write_pyproject(tmp_path, _SYNTHETIC_PYPROJECT_WITH_MCP)
    tests_dir = _make_tests_tree(
        tmp_path, "test_a.py",
        'import pytest\npytest.importorskip("mcp.sub")\n',
    )
    offenders = offending_calls(tests_dir, pyproject)
    assert offenders == [(tests_dir / "test_a.py", 2, "mcp.sub")]


def test_bare_importorskip_after_from_pytest_import_is_also_flagged(
    tmp_path: Path,
) -> None:
    """Tier 2: ``from pytest import importorskip; importorskip("x")`` — a
    bare ``Name`` call, not an ``Attribute`` one — is the same mechanism
    under a different import shape and must be caught the same way."""
    pyproject = _write_pyproject(tmp_path, _SYNTHETIC_PYPROJECT_WITH_MCP)
    tests_dir = _make_tests_tree(
        tmp_path, "test_a.py",
        "from pytest import importorskip\nimportorskip('mcp')\n",
    )
    offenders = offending_calls(tests_dir, pyproject)
    assert offenders == [(tests_dir / "test_a.py", 2, "mcp")]


def test_an_optional_extra_importorskip_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: ACCEPT SIDE (lead-coder's explicit requirement) — a package
    NOT in pyproject.toml's core dependencies (an optional extra, a dev
    tool, anything else) keeps using ``importorskip`` correctly and must
    stay green under this gate."""
    pyproject = _write_pyproject(tmp_path)
    tests_dir = _make_tests_tree(
        tmp_path, "test_a.py",
        'import pytest\npytest.importorskip("some_optional_extra", reason="fine")\n',
    )
    offenders = offending_calls(tests_dir, pyproject)
    assert offenders == []


def test_a_docstring_mention_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: the actual false-positive class #5058's own manual census
    had to hand-filter — this repo's real
    ``tests/interfaces/test_5051_web_core_deps_import.py`` says
    ``pytest.importorskip("fastapi", ...)`` in its OWN docstring, never
    calls it. AST-based detection (real ``ast.Call`` nodes only) cannot
    mistake a string literal for a call, by construction — no regex/text
    scan involved."""
    pyproject = _write_pyproject(tmp_path, _SYNTHETIC_PYPROJECT_WITH_MCP)
    tests_dir = _make_tests_tree(
        tmp_path, "test_a.py",
        '"""Some files use ``pytest.importorskip("mcp", ...)``."""\n'
        "X = 1\n",
    )
    offenders = offending_calls(tests_dir, pyproject)
    assert offenders == []


def test_the_real_repo_tree_is_currently_clean() -> None:
    """Tier 2: the gate's own starting population, verified against the
    real, current tree (not assumed) — mirrors
    check_bare_tests_import_reference.py's own "run it before shipping
    it" discipline. #5058's own cleanup (17 fastapi files + 12 mcp files,
    fixed across this issue's thread) is what makes this zero; any hit
    here is a NEW regression, not inherited debt."""
    assert _TESTS_DIR == _ROOT / "tests"
    offenders = offending_calls(_TESTS_DIR, _ROOT / "pyproject.toml")
    assert offenders == [], (
        f"real regression(s) found: {offenders} -- this gate's baseline is "
        "zero, so any hit here is new, not inherited debt"
    )
