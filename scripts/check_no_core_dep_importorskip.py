#!/usr/bin/env python3
"""#5058 — a ``pytest.importorskip`` naming a CORE dependency turns a broken
install into a silent skip (CLAUDE.md's six-questions Q4: "would it stay
green having never run?"). ``importorskip`` means "this dependency is
optional, skip if absent" — that declaration is correct for an ``extra``,
wrong for anything in ``pyproject.toml``'s ``[project].dependencies``: its
absence there is not a normal configuration, it is a broken install, and
the correct behaviour is a loud collection failure, not a green skip.

17 ``fastapi``/``starlette``/``uvicorn``/``websockets`` files (#5051) and 12
``mcp`` files were found and fixed by hand across this issue's own thread —
the class, not the instances, is what this gate closes: a FUTURE core-dep
``importorskip`` (a new file, or one reintroduced by a revert) must be
caught here, not found again by a human census.

## The classifier: declaration-derived, not test-file-derived

(architect's ruling, #5058) the judgment "is this dependency core" comes
from ONE source — ``pyproject.toml``'s own ``[project].dependencies`` — never
from anything the test file itself says (a test's own ``reason=`` string is
not evidence; #5058's own thread found one that was already stale/false).

## The name-mismatch trap (architect, #5058) — and how this gate stays honest

A distribution name is not always its import name (``pillow`` imports as
``PIL``; ``pyyaml`` imports as ``yaml``). A NAIVE derivation (lowercase +
hyphen-to-underscore) would silently miss those two today, and would
silently miss whatever the NEXT mismatch is when a new core dependency
is declared. So this gate does not derive an import name — it looks one
up in :data:`IMPORT_NAME_MANIFEST`, an EXPLICIT, exhaustive table (every
current core dependency has an entry, not just the mismatched ones), and
FAILS LOUD (a distinct failure mode from the usual scan, see ``main()``)
if ``pyproject.toml`` ever names a core dependency this table does not
cover — "increased -> decide", never "increased -> silently pass" (the
same shape #5944's byte-cap alias table would have needed had grep's
match targets ever needed one). Scope explicitly declared: this manifest
covers each dependency's PRIMARY import name(s) only — a package that
exposes read-only compatibility shims under other top-level names is out
of this gate's reach unless added here.

## What counts as "using" a name (AST, not text)

A real ``ast.Call`` node whose function is ``pytest.importorskip`` (the
overwhelmingly common shape in this repo) or a bare ``importorskip`` (the
shape after ``from pytest import importorskip``), with the checked
package name as its first positional string-literal argument. This is
deliberately NOT a text/regex scan: #5058's own manual census had to
hand-filter prose/docstring hits that merely NAME the pattern (this very
module's sibling, ``tests/interfaces/test_5051_web_core_deps_import.py``,
says ``pytest.importorskip("fastapi", ...)`` in its OWN docstring, never
calls it) — ``ast.walk`` only ever sees real calls, so that class of false
positive cannot occur here by construction.

## Scope: only ``tests/`` (extras stay green)

An ``importorskip`` naming an OPTIONAL extra (``watchdog``, ``linebot.v3``,
``slack_bolt``, ...) is the CORRECT use of the mechanism and must keep
passing — this gate's only job is the core/non-core distinction, so a
package name that resolves to no manifest entry (because it names
something outside ``[project].dependencies`` entirely — an extra, a dev
tool, a test-only package) is silently fine, by construction: it is
never even checked against the manifest-completeness failure mode, which
only walks ``pyproject.toml``'s own core list.

CI: gate
"""
from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _ROOT / "tests"
_PYPROJECT = _ROOT / "pyproject.toml"

# Every CURRENT `[project].dependencies` entry's distribution name (lowercased,
# as extracted by `core_dependency_names`) -> its acceptable top-level import
# name(s). Exhaustive by design (see module docstring) -- `main()` fails loud
# if `pyproject.toml` names a core dependency missing from this table, rather
# than silently treating an unlisted-but-actually-core package as non-core.
IMPORT_NAME_MANIFEST: dict[str, frozenset[str]] = {
    "litellm": frozenset({"litellm"}),
    "pydantic": frozenset({"pydantic"}),
    "jsonschema": frozenset({"jsonschema"}),
    "setproctitle": frozenset({"setproctitle"}),
    "pyyaml": frozenset({"yaml"}),  # the mismatch architect named
    "prompt_toolkit": frozenset({"prompt_toolkit"}),
    "ddgs": frozenset({"ddgs"}),
    "rich": frozenset({"rich"}),
    "textual": frozenset({"textual"}),
    "textual-flowview": frozenset({"textual_flowview"}),
    "numpy": frozenset({"numpy"}),
    "croniter": frozenset({"croniter"}),
    "charset-normalizer": frozenset({"charset_normalizer"}),
    "mcp": frozenset({"mcp"}),
    "anyio": frozenset({"anyio"}),
    "httpx": frozenset({"httpx"}),
    "cryptography": frozenset({"cryptography"}),
    "pyperclip": frozenset({"pyperclip"}),
    "pillow": frozenset({"PIL"}),  # the other mismatch architect named
    "fastapi": frozenset({"fastapi"}),
    "starlette": frozenset({"starlette"}),
    "uvicorn": frozenset({"uvicorn"}),
    "websockets": frozenset({"websockets"}),
}

# A PEP 508 requirement string's name ends at the first of: an extras
# bracket, a version/marker operator, an environment-marker `;`, or a
# direct-URL `@` (`"textual-flowview @ git+https://...")`) -- whichever
# comes first, or whitespace before any of those.
_NAME_BOUNDARY = re.compile(r"[\[<>=~! @;]")


def core_dependency_names(pyproject_path: Path = _PYPROJECT) -> "list[str]":
    """Every ``[project].dependencies`` entry's distribution name, lowercased
    -- the SINGLE source of truth this gate's classifier reads (architect's
    ruling: never derive "is this core" from a test file)."""
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    raw = data.get("project", {}).get("dependencies", [])
    return [_NAME_BOUNDARY.split(entry, 1)[0].lower() for entry in raw]


def manifest_gaps(core_deps: "list[str]") -> "list[str]":
    """Core dependency names ``IMPORT_NAME_MANIFEST`` has no entry for --
    non-empty means the manifest is stale (a new core dependency was
    declared and nobody added its import name here). This is a DIFFERENT
    failure from the usual scan: it means the gate cannot currently judge
    every core dependency, not that a violation was found."""
    return [name for name in core_deps if name not in IMPORT_NAME_MANIFEST]


def core_import_names(core_deps: "list[str]") -> "set[str]":
    """The set of top-level import names that name a CURRENT core
    dependency, per the manifest. Callers should check :func:`manifest_gaps`
    first -- a name missing from the manifest is silently absent here too,
    which is exactly the failure mode ``main()`` treats separately."""
    names: set[str] = set()
    for dep in core_deps:
        names |= IMPORT_NAME_MANIFEST.get(dep, frozenset())
    return names


def _importorskip_calls(path: Path) -> "list[tuple[int, str]]":
    """``(lineno, checked_name)`` for every REAL ``pytest.importorskip(...)``
    / bare ``importorskip(...)`` call in *path* whose first positional
    argument is a string literal -- an ``ast.Call`` node, never a text/regex
    match, so a docstring or comment merely naming the pattern (this repo
    has one, see module docstring) cannot appear here."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_importorskip = (
            (isinstance(func, ast.Attribute) and func.attr == "importorskip")
            or (isinstance(func, ast.Name) and func.id == "importorskip")
        )
        if not is_importorskip or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            hits.append((node.lineno, first.value))
    return hits


def offending_calls(
    tests_dir: Path = _TESTS_DIR, pyproject_path: Path = _PYPROJECT
) -> "list[tuple[Path, int, str]]":
    """``(file, lineno, argument)`` for every ``importorskip`` call whose
    checked package's TOP-LEVEL name (``"mcp.server"`` -> ``"mcp"``) is a
    core dependency's import name. Isolated from CLI/printing so it is
    directly testable (mirrors ``check_bare_tests_import_reference.py``'s
    own ``offending_files`` split)."""
    core_names = core_import_names(core_dependency_names(pyproject_path))
    offenders: list[tuple[Path, int, str]] = []
    for path in sorted(tests_dir.rglob("*.py")):
        for lineno, arg in _importorskip_calls(path):
            top = arg.split(".", 1)[0]
            if top in core_names:
                offenders.append((path, lineno, arg))
    return offenders


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options -- a whole-tree scan against a baseline of zero

    core_deps = core_dependency_names(_PYPROJECT)
    gaps = manifest_gaps(core_deps)
    if gaps:
        print(
            "no-core-dep-importorskip gate FAILED (manifest incomplete):\n",
            file=sys.stderr,
        )
        print(
            f"pyproject.toml declares {len(gaps)} core dependenc"
            f"{'y' if len(gaps) == 1 else 'ies'} with no entry in this "
            "script's IMPORT_NAME_MANIFEST -- the gate cannot judge "
            "importorskip calls against a dependency it cannot map to an "
            "import name:",
            file=sys.stderr,
        )
        for name in gaps:
            print(f"  {name!r}", file=sys.stderr)
        print(
            "\nAdd each one to IMPORT_NAME_MANIFEST in "
            "scripts/check_no_core_dep_importorskip.py (its top-level "
            "import name -- check whether it differs from the "
            "distribution name, e.g. pillow -> PIL) before this gate can "
            "run.",
            file=sys.stderr,
        )
        return 1

    offenders = offending_calls(_TESTS_DIR, _PYPROJECT)
    if not offenders:
        print("OK: no importorskip() names a core dependency.")
        return 0

    print("no-core-dep-importorskip gate FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} importorskip() call(s) name a CORE dependency "
        "(pyproject.toml [project].dependencies) -- its absence is a "
        "broken install, not a normal missing-extra path, so the correct "
        "behavior is a loud collection failure, not a silent skip (#5058):",
        file=sys.stderr,
    )
    for path, lineno, arg in offenders:
        rel = path.relative_to(_ROOT)
        print(f"  {rel}:{lineno}: importorskip({arg!r}, ...)", file=sys.stderr)

    print(
        "\nRemove the guard (a plain `import` at module/function scope is "
        "the correct replacement -- a missing core dependency should now "
        "raise ModuleNotFoundError and fail collection loudly). An "
        "OPTIONAL extra's importorskip is unaffected by this gate -- only "
        "names in pyproject.toml's core dependencies are flagged.\n"
        "\nThis gate's own starting population is zero (#5058's own "
        "cleanup), so any hit here is a new regression, not inherited debt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
