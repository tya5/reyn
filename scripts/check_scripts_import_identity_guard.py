#!/usr/bin/env python3
"""#3024 — every bare-python `scripts/*.py` file that imports `reyn`
(module-level OR lazily, anywhere in the file) must call
`verify_env_identity.guard_bare_script_or_exit()` before any code path can
reach that import, or be named — with a reason — in `EXEMPT_SCRIPTS` below.

## Why this exists

Tonight's venv-identity sweep (#3024's own family) found that `pytest`'s
own in-process resolution is protected (root `conftest.py`'s
`pytest_configure`, #3233) but a BARE `python scripts/foo.py` invocation
is not: one session's venv had an editable `.pth` whose dictionary order
resolved `reyn` to a DIFFERENT checkout entirely for any bare-python
script, while `pytest` in the SAME venv read the correct tree (pytest's
own `pythonpath = ["src"]` wins ahead of the stale `.pth`). "pytest is
green" was never evidence a bare-python gate script was measuring the
right tree — the two resolve independently.

## Population: derived, never a hand-typed list

Every top-level `scripts/*.py` file is parsed with `ast` and walked for
ANY `Import`/`ImportFrom` node naming `reyn` or a `reyn.*` submodule, at
ANY nesting depth (module level, inside a function, behind a CLI flag —
the guard call's whole point, per its own docstring, is that ONE call at
a script's entry point covers every later `import reyn` regardless of
where it lives, so the population this gate checks is symmetric: every
site that WOULD need covering, not just the module-level ones). A string
literal containing the text "from reyn" (e.g. a script that builds a
`python -c` payload as an f-string for a SEPARATE subprocess to run —
`s7_driver.py` / `rekey_fixtures.py`, both measured and excluded this way)
is correctly NOT counted: the AST only sees real `Import`/`ImportFrom`
nodes, never string contents, so a script whose own process never imports
`reyn` is not in this gate's population at all — it does not need the
guard, and listing it as an exemption would misstate WHY it is absent.

## Exemption: a reasoned table, not a silent gap

`EXEMPT_SCRIPTS` names every population member that deliberately carries
NO guard call, each with a 1-line reason — never a bare filename list a
future reader has to re-derive the reasoning for. The shape mirrors
`exec_wrappers.py`'s own `UNSUPPORTED_WRAPPERS` (#6061): the population
this gate measures is honest about what it does NOT enforce, in the same
place it enforces the rest, rather than a hand-maintained allowlist a
new wheel-mode probe or A/B harness would have to remember to update
somewhere else.

Today's 3 entries are all the SAME shape: a script whose entire purpose
is comparing `reyn` as resolved from two DIFFERENT trees (a dev checkout
vs. an installed wheel, or two different `PYTHONPATH` arms) cannot also
assert it is only ever pointed at one — the guard's own contract is
exactly the opposite assertion these scripts exist to test.

## What this gate does NOT verify

Call PRESENCE only — not that the call is reached before every import
site (a `guard_bare_script_or_exit()` call placed AFTER a script's own
`import reyn` would satisfy this gate's own AST-presence check while
still being too late in a real run). This is the same class of ceiling
`check_ci_role_declared.py`'s own docstring names for its "wired, but
does the role's obligation actually hold" question — mechanically
verifiable placement (first-statement-after-docstring) is a possible
future tightening, not attempted here; a PR review is the check today.

CI: gate
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCOPE = "scripts"

#: Every population member that deliberately carries no
#: `guard_bare_script_or_exit()` call, with why. See module docstring,
#: "Exemption" — a reasoned table, never a silent gap.
EXEMPT_SCRIPTS: "dict[str, str]" = {
    "df2187_harness.py": (
        "A/B driver comparing the SAME harness run against two different "
        "PYTHONPATH arms (its own docstring: 'A/B by codebase on the "
        "import path') -- a script whose job is diffing two trees cannot "
        "also assert it is only ever pointed at one."
    ),
    "wheel_parity_probe.py": (
        "Runs inside a throwaway wheel-only venv and asserts `reyn` "
        "resolves the INSTALLED WHEEL, never `<root>/src` -- the guard's "
        "own contract is the opposite of what this probe exists to prove."
    ),
    "wheel_plugin_install_probe.py": (
        "Same wheel-only-venv shape as wheel_parity_probe.py -- `reyn` "
        "MUST resolve site-packages here, never a dev checkout's src/."
    ),
}


def _iter_scan_files(root: Path = _ROOT) -> "list[Path]":
    """Every tracked top-level `.py` file directly under `scripts/` —
    same population source (`git ls-files`) `silent_except_ratchet.py`/
    `user_facing_lang_gate.py` use, for the same reason: no hand-maintained
    exclusion list. Non-recursive (`scripts/*.py`, not `scripts/**/*.py`)
    -- `scripts/` carries no subpackages today; this gate's OWN population
    derivation (like the guard mechanism it checks for) would need to
    widen the same day a `scripts/<subdir>/` appears, not before."""
    proc = subprocess.run(
        ["git", "ls-files", "--", f"{_SCOPE}/*.py"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [root / line for line in proc.stdout.splitlines() if line.strip()]


def imports_reyn(path: Path) -> bool:
    """True iff *path* contains a real `Import`/`ImportFrom` AST node
    naming `reyn` or a `reyn.*` submodule, at ANY nesting depth (module
    level or lazy/nested) — never a string-literal match, so a script that
    only builds `python -c`-style source text for a SEPARATE subprocess
    (never importing `reyn` in ITS OWN process) is correctly excluded."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "reyn" or a.name.startswith("reyn.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "reyn" or node.module.startswith("reyn.")):
                return True
    return False


def calls_guard(path: Path) -> bool:
    """True iff *path* contains a call to `guard_bare_script_or_exit` —
    presence only, see module docstring's own "What this gate does NOT
    verify" section for the placement caveat."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else None)
            if name == "guard_bare_script_or_exit":
                return True
    return False


def measured(root: Path = _ROOT) -> "tuple[list[str], list[str], int]":
    """`(missing, stale_exemptions, scanned)` — *missing* names population
    members with no guard call and no exemption entry; *stale_exemptions*
    names `EXEMPT_SCRIPTS` entries whose file no longer imports `reyn` at
    all (the exemption's own reasoning no longer applies — a genuine
    finding, not a false pass: an exemption for a script that has stopped
    needing one is itself drift). A file that fails to parse propagates
    the `SyntaxError` — fail-closed, same contract every AST-based gate in
    this repo shares (a silent under-count here is the exact defect this
    gate exists to prevent, one layer up)."""
    files = _iter_scan_files(root)
    by_name = {f.name: f for f in files}
    missing: "list[str]" = []
    for f in files:
        if f.name == "verify_env_identity.py" or f.name == Path(__file__).name:
            continue  # the guard's own implementation, and this gate itself -- never import reyn
        if not imports_reyn(f):
            continue
        if f.name in EXEMPT_SCRIPTS:
            continue
        if not calls_guard(f):
            missing.append(f.name)

    stale_exemptions = [
        name for name in EXEMPT_SCRIPTS
        if name in by_name and not imports_reyn(by_name[name])
    ]
    return (missing, stale_exemptions, len(files))


def main(argv: "list[str] | None" = None) -> int:
    del argv
    try:
        missing, stale_exemptions, scanned = measured(_ROOT)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        print(
            f"check_scripts_import_identity_guard FAILED: could not scan the "
            f"population ({exc}). Fails CLOSED on a scan error rather than "
            f"silently under-counting.",
            file=sys.stderr,
        )
        return 1

    if scanned == 0:
        print(
            f"check_scripts_import_identity_guard FAILED: the scan found 0 "
            f"files under {_SCOPE}/ -- a scanner failure, not a clean "
            "population; `git ls-files` returned nothing.",
            file=sys.stderr,
        )
        return 1

    ok = True
    if missing:
        ok = False
        print(
            f"check_scripts_import_identity_guard FAILED: {len(missing)} "
            f"script(s) under {_SCOPE}/ import `reyn` with no "
            "`guard_bare_script_or_exit()` call and no EXEMPT_SCRIPTS entry:",
            file=sys.stderr,
        )
        for name in sorted(missing):
            print(f"  {name}", file=sys.stderr)
        print(
            "\nAdd `from verify_env_identity import guard_bare_script_or_exit` "
            "+ a `guard_bare_script_or_exit()` call near the top of the file "
            "(before any of its own `import reyn`), or -- if this script "
            "deliberately compares/expects a DIFFERENT tree -- add it to "
            "EXEMPT_SCRIPTS in this gate's own source, with a reason.",
            file=sys.stderr,
        )
    if stale_exemptions:
        ok = False
        print(
            f"check_scripts_import_identity_guard FAILED: "
            f"{len(stale_exemptions)} EXEMPT_SCRIPTS entry(ies) no longer "
            "import `reyn` at all -- the exemption's own reason no longer "
            "applies:",
            file=sys.stderr,
        )
        for name in sorted(stale_exemptions):
            print(f"  {name}", file=sys.stderr)
        print(
            "\nRemove the stale entry from EXEMPT_SCRIPTS.",
            file=sys.stderr,
        )

    if not ok:
        return 1

    print(
        f"check_scripts_import_identity_guard OK: scanned {scanned} file(s) "
        f"under {_SCOPE}/, every reyn-importing script guarded or reasoned-exempt."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
