#!/usr/bin/env python3
"""Regression-prevention gate for #5978: the set of modules that call
``faulthandler``'s timer API (``faulthandler.dump_traceback_later(...)`` /
``faulthandler.cancel_dump_traceback_later()``) must be EXACTLY
``{src/reyn/runtime/stall_trace.py}`` -- no other module may call either
function.

**This gate starts already GREEN.** It is a regression ratchet, not a
cleanup -- nothing in the tree is fixed by adding it. Read-only measurement
against ``origin/main`` at the time this gate was written:

```
$ git grep -nE '^[^#]*faulthandler\\.(dump_traceback_later|cancel_dump_traceback_later)\\(' origin/main -- src
src/reyn/runtime/stall_trace.py:284
src/reyn/runtime/stall_trace.py:291
```

Exactly 2 call sites, both already inside the one sanctioned caller. This
gate exists to catch a SECOND caller being ADDED in the future, not to
remove one that exists today.

## Why this gate, and not a broader one (#5978's settled scope)

#5978 originally asked for something broader: derive the population of
ALL diagnostic write-outs (snapshots/streams/dumps) and bound each one
structurally. The architect's ruling on that issue found that goal NOT
achievable -- write-mode opens (`os.open` / `open(..., "a"|"w")`) are
scattered across 20+ modules mixing event-log records, caches, plugins,
and CI tooling, and "diagnostic vs. not" is a *purpose*, not something
written in the source; a gate over that population would need a
hand-maintained exclusion list, which is exactly what this repo's
"derive the population, never hand-maintain a table" discipline forbids.

What IS derivable is not the *purpose* but a *mechanism name* that
happens to have exactly one real caller today: ``faulthandler``'s timer
API. That gives a population with no exclusion list at all -- "every
module that calls this exact name" -- so this gate's scope is narrowed
to that one mechanism, per the issue thread's final agreement between
lead-coder and architect (see #5978's last two comments).

## Population and detection

Population: every tracked ``.py`` file under ``src/`` (``git ls-files``,
no hand-maintained exclusion list -- the same population-derivation
pattern as ``check_unfiltered_caplog_consumption.py`` /
``silent_except_ratchet.py``). Detection: an AST walk for a ``Call`` node
whose function is an attribute access ``faulthandler.dump_traceback_
later`` or ``faulthandler.cancel_dump_traceback_later`` where the base
name is literally ``faulthandler`` (i.e. the module was imported as
``import faulthandler`` and called via that name, the only import shape
this repo uses for it). This is a syntax-shape classifier, not dataflow --
an aliased import (``import faulthandler as fh``) or an indirection
through a rebound reference would not be caught; no such shape exists in
the current tree (verified by the grep above, which has the same blind
spot and still found exactly 2 lines).

## CI wiring

Runs on every push to ``src/**`` (not gated to ``tests/**`` -- this
gate's population lives entirely under ``src/``, and the population is
tiny, so a plain ``src/**``-path trigger is the lightest correct choice;
there is no reason to also run it on doc-only or tests-only changes).

CI: gate
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCOPE = "src"

_TIMER_API_NAMES = frozenset({"dump_traceback_later", "cancel_dump_traceback_later"})
_SOLE_CALLER = "src/reyn/runtime/stall_trace.py"


def _iter_scan_files(root: Path = _ROOT) -> "list[Path]":
    """Every tracked `.py` file under `src/` -- `git ls-files`, so the
    population is exactly what's checked in and nothing is hand-excluded."""
    proc = subprocess.run(
        ["git", "ls-files", "--", f"{_SCOPE}/*.py"],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return [root / line for line in proc.stdout.splitlines() if line.strip()]


def _is_faulthandler_timer_call(node: ast.AST) -> bool:
    """True for a `Call` node shaped `faulthandler.dump_traceback_later(...)`
    or `faulthandler.cancel_dump_traceback_later(...)` -- a real `ast.Call`
    over an `ast.Attribute` whose base is the bare name `faulthandler`,
    never a text/regex match, so a docstring or comment merely naming the
    API cannot trigger this."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _TIMER_API_NAMES
        and isinstance(func.value, ast.Name)
        and func.value.id == "faulthandler"
    )


def find_violations(path: Path) -> "list[tuple[int, str]]":
    """`(lineno, api_name)` for every `faulthandler` timer-API call site in
    *path*."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    violations: "list[tuple[int, str]]" = []
    for node in ast.walk(tree):
        if _is_faulthandler_timer_call(node):
            attr = node.func.attr  # type: ignore[attr-defined]
            violations.append((node.lineno, attr))
    return sorted(violations)


def measured(root: Path = _ROOT) -> "dict[str, list[tuple[int, str]]]":
    """File (relative to *root*) -> call sites, for every tracked `src/`
    file with at least one `faulthandler` timer-API call -- including the
    one sanctioned caller. Callers that want only the VIOLATIONS should
    exclude `_SOLE_CALLER` from the result (see `main`)."""
    by_file: "dict[str, list[tuple[int, str]]]" = {}
    for path in _iter_scan_files(root):
        violations = find_violations(path)
        if violations:
            by_file[str(path.relative_to(root))] = violations
    return by_file


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options -- population has no exclusion list to configure

    try:
        by_file = measured(_ROOT)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        print(
            f"faulthandler-timer-api-single-caller gate FAILED: could not "
            f"scan the population ({exc}). Fails CLOSED on a scan error "
            "rather than silently under-counting.",
            file=sys.stderr,
        )
        return 1

    if not by_file:
        print(
            "faulthandler-timer-api-single-caller gate FAILED: found 0 "
            f"callers of faulthandler's timer API under {_SCOPE}/ at all -- "
            f"expected exactly one, {_SOLE_CALLER}. Either the sole caller "
            "was removed (update this gate's expectation deliberately) or "
            "the scan itself is broken.",
            file=sys.stderr,
        )
        return 1

    others = {f: v for f, v in by_file.items() if f != _SOLE_CALLER}
    if others:
        total = sum(len(v) for v in others.values())
        print(
            "faulthandler-timer-api-single-caller gate FAILED:\n",
            file=sys.stderr,
        )
        print(
            f"{total} faulthandler timer-API call site(s) outside the one "
            f"sanctioned caller ({_SOLE_CALLER}). The invariant (#5978) is "
            "that ONLY that module may call "
            "faulthandler.dump_traceback_later()/cancel_dump_traceback_"
            "later() -- it owns the one process-wide timer and documents "
            "its own re-arm/handoff contract; a second independent caller "
            "would silently race it for the same global timer:",
            file=sys.stderr,
        )
        for file, violations in sorted(others.items()):
            for lineno, api in violations:
                print(f"  {file}:{lineno}: faulthandler.{api}(...)", file=sys.stderr)
        print(
            f"\nRoute the new call through {_SOLE_CALLER} instead (add a "
            "function there, or call its existing arm()/disarm()) rather "
            "than calling faulthandler's timer API directly.",
            file=sys.stderr,
        )
        return 1

    print(
        f"faulthandler-timer-api-single-caller gate OK: {_SOLE_CALLER} is "
        f"the only caller of faulthandler's timer API under {_SCOPE}/ "
        f"({len(by_file[_SOLE_CALLER])} call site(s), as expected)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
