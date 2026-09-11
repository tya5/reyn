#!/usr/bin/env python3
"""#6151 — a test-side ``Session(...)`` construction that omits
``child_temp_dir=`` resolves ``Session._child_temp_dir`` to
``<tempdir>/reyn/<agent_name>/<session_id>`` -- a REAL, SHARED filesystem
path. Two of this repo's own sanctioned test helpers,
``tests/_support/agent_session.py::make_session`` and
``tests/_support/session.py::make_session``, both default their own
``agent_name``/``session_id`` to fixed literals ("alpha"/"main",
"default"/"main") across hundreds of call sites -- #6151 measured
``pytest -n auto`` workers racing (create/delete/permission) on that ONE
shared directory across unrelated tests in different worker processes,
producing intermittent, hard-to-reproduce CI failures.

Both helpers now isolate this by DEFAULT (an ``isolate_child_temp_dir``
kwarg, default ``True``, injects a fresh ``tempfile.mkdtemp()``-based
``child_temp_dir`` per call unless the caller explicitly opts out or
supplies their own). This gate closes the SECOND entry point: a test
file that constructs ``reyn.runtime.session.Session`` DIRECTLY (bypassing
both helpers) gets none of that automatic protection -- #6151's own
investigation found one such site (``test_skill_invoke_3100.py``'s
``_session_with_skills``, itself a documented copy-paste of the
``tests/_support/session.py`` helper body, #3413) already exposed to the
exact same shared-path shape.

## What counts as a violation

A real ``ast.Call`` whose function resolves to the bare name ``Session``
(the overwhelmingly common import shape, ``from reyn.runtime.session
import Session``) inside ``tests/`` -- excluding this repo's own two
sanctioned helper files, which are the fix's own home, not a caller of
it -- that has NO ``child_temp_dir=`` keyword argument among its call
args. A ``Session(...)`` call is deliberately NOT matched by a dotted
name (``session.Session(...)``) or an aliased import -- this gate's own
starting population is the ONE real site #6151 already found and fixed
(re-verified empty as of this gate's own authoring), so a future
regression of either shape is what this exists to catch, not a claim of
exhaustive detection against every possible import alias.

CI: gate
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _ROOT / "tests"

# The fix's own home -- these two helpers ARE the isolation mechanism
# (their own `Session(...)` call already threads `child_temp_dir=`
# through their own `isolate_child_temp_dir` param), not a caller this
# gate should flag.
_EXEMPT_FILES = frozenset({
    _TESTS_DIR / "_support" / "agent_session.py",
    _TESTS_DIR / "_support" / "session.py",
})


def _is_session_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Session"


def _has_child_temp_dir_kwarg(call: ast.Call) -> bool:
    return any(kw.arg == "child_temp_dir" for kw in call.keywords)


def offending_calls(tests_dir: Path = _TESTS_DIR) -> "list[tuple[Path, int]]":
    """Every direct ``Session(...)`` call under ``tests/`` (outside the 2
    exempt helper files) missing ``child_temp_dir=``."""
    offenders: "list[tuple[Path, int]]" = []
    for path in sorted(tests_dir.rglob("*.py")):
        if path in _EXEMPT_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if _is_session_call(node) and not _has_child_temp_dir_kwarg(node):
                offenders.append((path, node.lineno))
    return offenders


def main() -> int:
    offenders = offending_calls()
    if not offenders:
        print("OK: no direct Session(...) construction in tests/ omits child_temp_dir=.")
        return 0

    print("session-child-temp-dir-isolation gate FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} direct Session(...) construction(s) in tests/ omit "
        "child_temp_dir= -- each resolves to a SHARED real filesystem path "
        "(<tempdir>/reyn/<agent_name>/<session_id>) that can collide with "
        "an unrelated test in a different `pytest -n auto` worker (#6151):",
        file=sys.stderr,
    )
    for path, lineno in offenders:
        rel = path.relative_to(_ROOT)
        print(f"  {rel}:{lineno}", file=sys.stderr)

    print(
        "\nFix: prefer tests/_support/agent_session.py::make_session or "
        "tests/_support/session.py::make_session (both isolate this by "
        "default, #6151) over a direct Session(...) construction. If a "
        "direct construction is genuinely required, pass your own "
        "child_temp_dir= (e.g. tempfile.mkdtemp(prefix='reyn-test-child-')) "
        "explicitly -- never the shared default.\n"
        "\nThis gate's own starting population is zero (#6151's own "
        "cleanup), so any hit here is a new regression, not inherited debt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
