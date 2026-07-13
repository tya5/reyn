"""Tier 2: proposal 0061 — repo self-access + packaging standardization.

Fast, in-process coverage of the dev==wheel parity invariant (§3.4) that
doesn't require building a real wheel (the heavier positive-reachability +
config-completeness half of the gate — building an actual wheel, installing
it into a clean venv, and reading README/docs/source through it
byte-identical to the dev checkout — lives in
``scripts/wheel_reachability_smoke.py``, run by
``.github/workflows/wheel-reachability.yml`` on every PR).

This file pins:

  1. **SSoT derivation, no drift (§3.3).** ``pyproject.toml``'s
     ``[tool.hatch.build.targets.wheel.force-include]`` table is generated
     from — and asserted here to still match — the single Python
     declaration in ``reyn.runtime.reyn_repo`` (``FORCE_INCLUDE_ENTRIES`` /
     ``REACHABLE_TOP_LEVEL_ENTRIES``). A hand-edit to either side that
     drifts from the other fails this test loudly instead of silently
     diverging (``preflight-gate-must-derive-path-from-ssot``).
  2. **NEGATIVE flip-witness (§3.4, required).** A non-declared path
     (``tests/...``, ``pyproject.toml``, ``scripts/...``) is refused in
     DEV mode — "equivalent to absent in the wheel" (those paths are
     never shipped). Falsified by hand during development: temporarily
     stripping the reachable-set gate out of ``safe_resolve_inside`` makes
     this exact test go RED (``DID NOT RAISE ValueError``) — see the PR
     description for the captured transcript
     (``bound-test-must-flip-under-strip``).
  3. **Wheel-mode logical<->physical translation**, exercised directly
     (without a real wheel) by monkeypatching ``_is_wheel_root`` to
     simulate wheel mode against a synthesized on-disk layout — proves
     the translation function itself, independent of the real-wheel
     smoke script.

No mocks of collaborators — real ``reyn.runtime.reyn_repo`` functions, real
repo/`pyproject.toml` content, and (for the wheel-mode translation test) a
real ``tmp_path`` on-disk layout standing in for an installed package dir.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from reyn.runtime.reyn_repo import (
    FORCE_INCLUDE_ENTRIES,
    REACHABLE_TOP_LEVEL_ENTRIES,
    SOURCE_LOGICAL_PREFIX,
    _translate_logical_to_physical,
    list_entries,
    read_text,
    resolve_reyn_root,
    safe_resolve_inside,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ── 1. SSoT derivation: pyproject.toml force-include matches the Python SSoT ─


def test_pyproject_force_include_matches_ssot() -> None:
    """Tier 2: pyproject.toml's force-include table has EXACTLY the keys in
    FORCE_INCLUDE_ENTRIES, each mapped to reyn/_bundled/<key> — the single
    declared reachable-set SSoT drives both sides, they cannot drift apart."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    assert set(force_include.keys()) == set(FORCE_INCLUDE_ENTRIES), (
        f"pyproject.toml force-include keys {set(force_include.keys())} "
        f"diverge from the SSoT {set(FORCE_INCLUDE_ENTRIES)}"
    )
    for key in FORCE_INCLUDE_ENTRIES:
        assert force_include[key] == f"reyn/_bundled/{key}", (
            f"force-include[{key!r}] = {force_include[key]!r}, expected "
            f"'reyn/_bundled/{key}'"
        )


def test_pyproject_packages_data_completeness_unchanged() -> None:
    """Tier 2: the 3 package-data omission-risk entries (0061 §3.1 — py.typed,
    builtin/**/*, environment/*.Dockerfile) are still declared, now under
    Hatchling's `artifacts` (not `[tool.setuptools.package-data]`)."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    artifacts = set(data["tool"]["hatch"]["build"]["targets"]["wheel"]["artifacts"])
    assert "src/reyn/py.typed" in artifacts
    assert "src/reyn/builtin/**/*" in artifacts
    assert "src/reyn/environment/*.Dockerfile" in artifacts


def test_build_backend_is_hatchling() -> None:
    """Tier 2: proposal 0061 §3.1 — setuptools -> Hatchling."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["build-system"]["build-backend"] == "hatchling.build"
    assert data["build-system"]["requires"] == ["hatchling"]
    assert not (_REPO_ROOT / "setup.py").exists(), "setup.py's build_py cmdclass is retired (0061 §3.1/§3.5)"


def test_mkdocs_docs_dir_unaffected() -> None:
    """Tier 2: force-include does not move docs/ off the repo root — mkdocs's
    `docs_dir: ../docs` (proposal 0061 §7 risk) must remain unaffected."""
    mkdocs_yml = (_REPO_ROOT / ".mkdocs" / "mkdocs.yml").read_text(encoding="utf-8")
    assert "docs_dir: ../docs" in mkdocs_yml
    assert (_REPO_ROOT / "docs").is_dir(), "docs/ must still live at the repo root"


# ── 2. NEGATIVE flip-witness: non-declared paths refused in dev ─────────────


@pytest.mark.parametrize(
    "non_declared_path",
    [
        "pyproject.toml",
        "CLAUDE.md",
        "tests/test_0061_repo_self_access_parity.py",
        "scripts/wheel_reachability_smoke.py",
        "dogfood/scenarios",
        "pipelines",
        "website",
    ],
)
def test_non_declared_path_refused_in_dev(non_declared_path: str) -> None:
    """Tier 2: (0061 §3.3/§3.4 flip-witness) a path outside the declared
    reachable set {README.md, CHANGELOG.md, docs, src} is refused in dev —
    "equivalent to absent in the wheel" (none of these ship in a wheel).

    Falsify (performed by hand during development, not re-run here): with
    the reachable-set gate stripped out of `safe_resolve_inside`, this
    exact assertion goes RED (`DID NOT RAISE ValueError`) because dev
    over-exposes `tests/`/`scripts/`/etc — see the PR description.
    """
    root = resolve_reyn_root()
    with pytest.raises(ValueError, match="reachable set"):
        safe_resolve_inside(root, non_declared_path)


def test_declared_paths_remain_reachable_in_dev() -> None:
    """Tier 2: sanity counterpart to the flip-witness above — the declared
    set itself is NOT accidentally over-narrowed."""
    root = resolve_reyn_root()
    for declared in ("README.md", "CHANGELOG.md", "docs", "src/reyn/runtime/reyn_repo.py"):
        target = safe_resolve_inside(root, declared)
        assert target.exists()


def test_list_root_shows_exactly_the_declared_set() -> None:
    """Tier 2: listing the top level ("") shows exactly
    REACHABLE_TOP_LEVEL_ENTRIES — not the dev checkout's whole top level
    (pyproject.toml, tests/, scripts/, .github/, ...)."""
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, "")
    result = list_entries(root, target, "")
    names = {e["name"] for e in result["entries"]}
    assert names == set(REACHABLE_TOP_LEVEL_ENTRIES)


# ── 3. Wheel-mode logical<->physical translation (no real wheel needed) ─────


def test_translate_logical_to_physical_is_never_invoked_in_dev_mode() -> None:
    """Tier 2: `_translate_logical_to_physical` always produces the WHEEL
    physical mapping (it has no mode branch itself) — `safe_resolve_inside`
    is what makes dev mode a no-op, by only invoking this function when
    `_is_wheel_root(root)`. Confirmed here: dev-mode reads resolve straight
    through (identity), even though the raw translation function itself
    would rewrite them."""
    root = resolve_reyn_root()
    # Dev-mode read of "docs/index.md" resolves to the literal repo path,
    # NOT the wheel-style "_bundled/docs/index.md" the bare translation
    # function would produce for the same logical input.
    target = safe_resolve_inside(root, "docs/index.md")
    assert target == (root / "docs" / "index.md").resolve()
    assert _translate_logical_to_physical("docs/index.md") == "_bundled/docs/index.md", (
        "the translation function itself is mode-agnostic; only its CALLER "
        "(safe_resolve_inside) makes it a no-op in dev mode"
    )


def test_translate_logical_to_physical_wheel_strips_source_prefix() -> None:
    """Tier 2: in wheel mode, `src/reyn/<x>` maps to `<x>` (the pinned
    canonical prefix, 0061 §7, stripped because the installed package
    directory already IS what `src/reyn/` names in dev)."""
    assert _translate_logical_to_physical(f"{SOURCE_LOGICAL_PREFIX}/runtime/reyn_repo.py") == (
        "runtime/reyn_repo.py"
    )
    assert _translate_logical_to_physical(SOURCE_LOGICAL_PREFIX) == ""


def test_translate_logical_to_physical_wheel_bundles_docs_and_readme() -> None:
    """Tier 2: in wheel mode, README/CHANGELOG/docs map under `_bundled/`
    (where Hatchling's force-include physically puts them)."""
    assert _translate_logical_to_physical("README.md") == "_bundled/README.md"
    assert _translate_logical_to_physical("CHANGELOG.md") == "_bundled/CHANGELOG.md"
    assert _translate_logical_to_physical("docs/index.md") == "_bundled/docs/index.md"


def test_safe_resolve_inside_simulated_wheel_mode_reads_correctly(tmp_path: Path) -> None:
    """Tier 2: end-to-end through `safe_resolve_inside` + `read_text` against
    a SYNTHESIZED on-disk layout standing in for an installed wheel package
    directory (`_bundled/README.md`, `_bundled/docs/x.md`, `runtime/y.py`
    directly under the package root) — proves the translation is wired into
    the resolver, not just unit-tested in isolation. No real wheel build
    needed for this fast in-process check (the real-wheel version lives in
    scripts/wheel_reachability_smoke.py)."""
    pkg_root = tmp_path / "reyn"
    (pkg_root / "_bundled" / "docs").mkdir(parents=True)
    (pkg_root / "runtime").mkdir(parents=True)
    (pkg_root / "_bundled" / "README.md").write_text("simulated readme\n")
    (pkg_root / "_bundled" / "docs" / "x.md").write_text("simulated doc\n")
    (pkg_root / "runtime" / "y.py").write_text("simulated source\n")

    # `_is_wheel_root` keys off `_bundled/` being adjacent — already true here.
    target = safe_resolve_inside(pkg_root, "README.md")
    assert read_text(target, "README.md")["content"] == "simulated readme\n"

    target = safe_resolve_inside(pkg_root, "docs/x.md")
    assert read_text(target, "docs/x.md")["content"] == "simulated doc\n"

    target = safe_resolve_inside(pkg_root, "src/reyn/runtime/y.py")
    assert read_text(target, "src/reyn/runtime/y.py")["content"] == "simulated source\n"

    # Non-declared path still refused even though it physically exists.
    (pkg_root / "setup_cfg_leftover.txt").write_text("nope\n")
    with pytest.raises(ValueError, match="reachable set"):
        safe_resolve_inside(pkg_root, "setup_cfg_leftover.txt")
