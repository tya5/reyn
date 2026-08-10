#!/usr/bin/env python3
"""A ``tests/<name>/`` directory name must not collide with real import
machinery, and (going forward) must mirror ``src/reyn/`` — #3879, lead-coder's
two follow-up corrections (broker 2026-08-09 01:20:15 / 01:20:32).

Two independent checks, both mechanical (never a declaration):

SHADOW (always active, retroactive — a live collision is bad regardless of
when it landed): a ``tests/<name>/`` directory that HAS an ``__init__.py``
makes it a REGULAR package. Python's import machinery merges same-named
NAMESPACE packages (PEP 420) across every ``sys.path`` entry, but a regular
package short-circuits that merge — it wins outright wherever it's found
first. pytest's own ``__init__.py``-chain walk inserts the directory ABOVE
the topmost ``__init__.py`` (here, ``tests/``) at the FRONT of ``sys.path``,
so once ``tests/<name>/__init__.py`` exists, ``import <name>`` resolves to
``tests/<name>/`` first — never merging with the real top-level package,
silently shadowing it. Verified directly (tui-coder, #3879 thread): adding
``tests/scripts/__init__.py`` broke collection of two existing tests that
``import scripts``.

★ "importable top-level name" here is NOT "resolves via
``importlib.util.find_spec``" — verified directly that EVERY non-hidden
repo-root directory resolves that way (PEP 420 namespace packages need no
``__init__.py`` and no content at all): ``dogfood``, ``pipelines``,
``docs``, ``website``, ``artifacts``, ``tmp`` all resolve via
``find_spec`` despite nothing in the codebase importing them as top-level
modules. Using that definition would flag nearly every repo-root directory
name as forbidden — including several of Stage 1's real destination
buckets that happen to share a word with a data directory. The definition
that actually matches the one confirmed real collision (``scripts``, 6
import-statement hits) and no false ones (0 hits for the rest) is
empirical: a name real code (``src/``, ``tests/``, ``scripts/``) actually
imports as a top-level module.

VOCABULARY (ratchet-style, forward-only — see ``flat_tests_ratchet.py``'s
docstring for why a ratchet, not a whitelist, is the right shape): the
directory-name SET currently on disk is the baseline (grandfathered —
several pre-#3879 directories, e.g. ``tests/chat``/``tests/cli``/
``tests/web``, share a name with an unrelated real ``src/reyn/<name>/``
package without actually testing it — confirmed by grep, not assumed: e.g.
``tests/cli/test_auth_login_ux.py`` imports ``reyn.interfaces.cli``, not
``reyn.cli`` — so rule ① alone cannot retroactively judge them, they
predate this gate entirely). A NEW top-level ``tests/<name>/`` directory
(one absent from the committed baseline) must satisfy BOTH:

  ① ``src/reyn/<name>/`` is a real package, OR ``name == "repo"`` (the
    special case for AST-guard/CI-structure tests that import zero
    ``reyn.*`` — no ``src/reyn/repo/`` exists or ever will), OR ``name``
    is one of a small explicit non-mirror allowlist (owner ruling via
    lead-coder, broker 2026-08-10, adopting option b over widening ① to
    "any coherent subject" — that would make the vocabulary check
    meaningless, since every flat file has SOME subject; an explicit
    allowlist keeps each new non-mirror name a reviewed gate edit instead
    of a silent accretion): ``intervention`` (``user_intervention.py`` /
    ``intervention_choices.py`` — the human-in-the-loop intervention
    mechanism's own shared vocabulary/type surface, both top-level
    single-file modules with no ``src/reyn/intervention/`` package to
    mirror) and ``http_safety`` (``_ssrf_guard.py`` / ``_ssrf_pin.py`` /
    ``_http_limits.py`` — the outbound-HTTP-request safety family: SSRF
    redirect-following denial, DNS-rebind connect-time IP pinning, and
    response-body byte ceiling; same shape, three top-level modules, no
    package).
  ② ``name`` is not one of the reserved legacy names (``scaffold``,
    ``_support`` — different axis, lifespan-scoped/non-test, not a
    src-mirror bucket at all; ``web``, ``cli``, ``chat`` — the
    name-coincidence orphans above, reserved so a NEW Stage-1 destination
    never reuses one of these misleading names by accident).

``--write-baseline`` and ``--check-growth`` mirror ``flat_tests_ratchet.py``
exactly, including the same hand-edit-the-baseline loophole and the same
guard against it.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _ROOT / "tests"
_SRC_REYN = _ROOT / "src" / "reyn"
_BASELINE_PATH = _ROOT / "scripts" / "tests_dir_names_baseline.json"

_REPO_SPECIAL_CASE = "repo"
# Reserved regardless of whether src/reyn/<name> exists (see module
# docstring's VOCABULARY section) — a NEW Stage-1 destination must not
# reuse one of these names.
_RESERVED_NAMES = frozenset({"scaffold", "_support", "web", "cli", "chat"})
# Explicit non-mirror allowlist for rule ① — see module docstring's
# VOCABULARY section for why each name is here and what it groups. Adding
# a name here is a reviewed gate edit (owner/lead-coder sign-off), never a
# baseline hand-edit — that's the whole point of an allowlist over
# widening rule ① itself.
_NON_MIRROR_ALLOWED_NAMES = frozenset({"intervention", "http_safety"})
# Directories the SHADOW/VOCABULARY scan itself must not treat as data:
# never a candidate `tests/<name>/` name, never counted as an "importable
# repo-root name" (the venv/build/vcs machinery, not code).
_NON_CODE_ROOT_DIRS = frozenset({
    ".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".serena", ".claude", ".github", ".mcp-sandbox", ".mkdocs", ".reyn",
    "__pycache__", "tests", "src",
})


def current_tests_dir_names(tests_dir: Path = _TESTS_DIR) -> "set[str]":
    """Top-level ``tests/<name>/`` directory names right now, excluding
    ``__pycache__``."""
    return {
        p.name for p in tests_dir.iterdir()
        if p.is_dir() and p.name != "__pycache__"
    }


def real_src_packages(src_reyn: Path = _SRC_REYN) -> "set[str]":
    """Every real top-level ``src/reyn/<name>/`` package (has
    ``__init__.py``) — the mirror-vocabulary a NEW ``tests/<name>/`` must
    match (rule ①)."""
    return {
        p.name for p in src_reyn.iterdir()
        if p.is_dir() and (p / "__init__.py").exists()
    }


def _top_level_import_names(source: str) -> "set[str]":
    """The root module name of every ``import``/``from ... import`` in
    *source* — ``import scripts.foo`` and ``from scripts.foo import bar``
    both yield ``"scripts"``. A source that fails to parse contributes
    nothing rather than aborting the whole scan."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def imported_top_level_names(root: Path = _ROOT) -> "set[str]":
    """Names real code (``src/``, ``tests/``, ``scripts/``) actually
    imports as a top-level module — see module docstring's SHADOW section
    for why this, not bare namespace-package resolvability, is the right
    definition."""
    names: set[str] = set()
    for sub in ("src", "tests", "scripts"):
        base = root / sub
        if not base.exists():
            continue
        for py_file in base.rglob("*.py"):
            try:
                source = py_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            names |= _top_level_import_names(source)
    return names


def repo_root_dir_names(root: Path = _ROOT) -> "set[str]":
    """Non-hidden, non-machinery top-level directory names — candidates for
    a real "repo-root importable name" collision (before intersecting with
    :func:`imported_top_level_names`)."""
    return {
        p.name for p in root.iterdir()
        if p.is_dir() and p.name not in _NON_CODE_ROOT_DIRS
    }


def shadowed_names(tests_dir: Path = _TESTS_DIR, root: Path = _ROOT) -> "set[str]":
    """``tests/<name>/`` directories that HAVE an ``__init__.py`` (making
    them a real package, not a namespace-package portion) whose ``name``
    collides with a repo-root name real code actually imports — see module
    docstring's SHADOW section for the exact mechanism this catches."""
    importable = imported_top_level_names(root) & repo_root_dir_names(root)
    return {
        p.name for p in tests_dir.iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and p.name in importable
    }


def is_allowed_new_name(name: str, src_packages: "set[str]") -> bool:
    """Rule ①+② for a NEW (not-yet-baselined) ``tests/<name>/`` — see
    module docstring's VOCABULARY section."""
    if name in _RESERVED_NAMES:
        return False
    return (
        name == _REPO_SPECIAL_CASE
        or name in src_packages
        or name in _NON_MIRROR_ALLOWED_NAMES
    )


def load_baseline(path: Path = _BASELINE_PATH) -> "set[str]":
    return set(json.loads(path.read_text(encoding="utf-8")))


def write_baseline(names: "set[str]", path: Path = _BASELINE_PATH) -> None:
    path.write_text(json.dumps(sorted(names), indent=2) + "\n", encoding="utf-8")


def new_dir_names(measured: "set[str]", baseline: "set[str]") -> "set[str]":
    """A directory name present now but absent from the baseline — a name
    leaving `measured` (a directory being retired) is not reported here at
    all, by design (same asymmetry as ``flat_tests_ratchet.py``)."""
    return measured - baseline


def baseline_at_ref(
    ref: str, path: Path = _BASELINE_PATH, root: Path = _ROOT,
) -> "set[str] | None":
    """The baseline's content as committed at *ref*, or ``None`` if the ref
    lacks the file entirely. *root* is an explicit parameter (not a
    hardcoded module constant) so this is testable against a throwaway git
    repo — same reasoning as ``flat_tests_ratchet.baseline_at_ref``."""
    rel = path.relative_to(root)
    proc = subprocess.run(
        ["git", "show", f"{ref}:{rel.as_posix()}"],
        cwd=root, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    return set(json.loads(proc.stdout))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=(
            "regenerate the vocabulary baseline from the CURRENT tests/*/ "
            "directory-name set instead of checking against it. Use ONLY "
            "for initial adoption or a real Stage-1 rename — regenerating "
            "to silence a new bad name defeats the ratchet."
        ),
    )
    parser.add_argument(
        "--check-growth",
        metavar="BASE_REF",
        help=(
            "additionally reject if the committed baseline itself grew "
            "versus BASE_REF — closes the hand-edit-the-baseline loophole, "
            "mirrors flat_tests_ratchet.py's --check-growth."
        ),
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    # Read the module globals by NAME here (not via callees' own default
    # parameter values, bound once at def-time) so a test can monkeypatch
    # _TESTS_DIR/_SRC_REYN/_BASELINE_PATH/_ROOT on this module and have
    # main() actually observe it (flat_tests_ratchet.py's fix for the same
    # bug, applied here from the start).
    measured = current_tests_dir_names(_TESTS_DIR)

    if args.write_baseline:
        write_baseline(measured, _BASELINE_PATH)
        print(f"Wrote {len(measured)} tests/ directory names to {_BASELINE_PATH}")
        return 0

    exit_code = 0

    # SHADOW — always active, regardless of baseline.
    shadowed = shadowed_names(_TESTS_DIR, _ROOT)
    if shadowed:
        exit_code = 1
        print("tests-dir-names SHADOW check FAILED:\n", file=sys.stderr)
        for name in sorted(shadowed):
            print(
                f"  tests/{name}/__init__.py shadows the real top-level "
                f"`{name}` package — pytest's rootdir insertion makes "
                f"`import {name}` resolve to tests/{name}/ first.",
                file=sys.stderr,
            )

    # VOCABULARY — ratchet, forward-only.
    baseline = load_baseline(_BASELINE_PATH)
    new = new_dir_names(measured, baseline)
    if new:
        src_packages = real_src_packages(_SRC_REYN)
        bad = {n for n in new if not is_allowed_new_name(n, src_packages)}
        if bad:
            exit_code = 1
            print("\ntests-dir-names VOCABULARY check FAILED:\n", file=sys.stderr)
            for name in sorted(bad):
                print(
                    f"  tests/{name}/ is new (not in the baseline) and is "
                    f"neither a real src/reyn/{name}/ package, `repo`, nor "
                    "outside the reserved-name list.",
                    file=sys.stderr,
                )
            print(
                "\nA NEW tests/<name>/ must mirror a real src/reyn/<name>/ "
                "package (or be `repo`) and must not be scaffold/_support/"
                "web/cli/chat. Do NOT add the name to the baseline to make "
                "this pass — see module docstring.",
                file=sys.stderr,
            )

    if args.check_growth:
        old = baseline_at_ref(args.check_growth, _BASELINE_PATH, _ROOT)
        if old is not None and len(baseline) > len(old):
            exit_code = 1
            added = baseline - old
            print(
                f"\ntests-dir-names VOCABULARY check FAILED: the baseline "
                f"itself grew ({len(old)} -> {len(baseline)} entries) "
                f"versus {args.check_growth} — hand-editing the baseline to "
                f"pre-authorize a bad name. New entries: {sorted(added)}",
                file=sys.stderr,
            )

    if exit_code == 0:
        print(
            f"tests-dir-names OK: {len(measured)} directories, 0 shadowed, "
            f"vocabulary baseline unmoved ({len(baseline)} declared)."
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
