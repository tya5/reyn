#!/usr/bin/env python3
"""Regression-prevention gate for #6050: the only place inside
``src/reyn/data/workspace/media_store.py`` allowed to call
``Path.unlink()`` is ``MediaStore._unlink_tracked`` itself.

**This gate starts already GREEN.** It is a regression ratchet, not a
cleanup — this PR itself collapsed the two pre-existing call sites
(``_evict_history_content_over_cap`` write-time-cap eviction,
``_evict_cross_session_over_cap`` project-wide eviction) into the one
sanctioned caller before adding this gate. Read-only measurement at the
time this gate was written:

```
$ git grep -nE '\\.unlink\\(' -- src/reyn/data/workspace/media_store.py
src/reyn/data/workspace/media_store.py:<lineno>:        path.unlink()
```

Exactly 1 call site, inside ``_unlink_tracked`` itself.

## Why this gate exists (#6050's own root cause)

Two eviction call sites each called ``path.unlink()`` directly and never
called ``.discard()`` on ``_history_content_spill_paths``/
``_unspilled_paths`` afterward — a file THIS process's own eviction
deleted stayed tracked as "known" forever. The fix (#6050) fuses
deletion and bookkeeping into one method (``_unlink_tracked``, which
calls ``self._forget(path)`` on every successful delete) so a FUTURE
3rd eviction path added to this class inherits correct bookkeeping by
construction, rather than needing to remember its own ``.discard()`` —
the exact thing both existing call sites independently forgot. This
gate is what stops a 3rd ``path.unlink()`` call site from reopening
that gap; without it, "route unlink through ``_unlink_tracked``" is a
convention someone has to remember, not something CI enforces (the
same "collapse the delete口 so it cannot be split apart again" shape
``#5978``/PR #6054 already established for ``faulthandler``'s timer
API — this gate's own structure mirrors that one directly).

## Population and detection

Population: the single file ``src/reyn/data/workspace/media_store.py``
(#6050's own scope is this one class, not a repo-wide sweep — a
broader "every ``Path.unlink()`` call anywhere in ``src/``" population
would need to distinguish this store's OWN tracked-path bookkeeping
concern from every other module's unrelated file-deletion, which is a
*purpose* judgement this gate does not attempt; see
``check_faulthandler_timer_api_single_caller.py``'s own docstring for
why a narrow, syntactically-derivable population beats a
hand-maintained exclusion list over a broad one). Detection: an AST
walk for a ``Call`` node whose function is an attribute access
``<expr>.unlink`` (i.e. ``.attr == "unlink"``, regardless of the base
expression — a bare ``Path.unlink()`` this file's own code always
writes as ``path.unlink()``, never aliased or wrapped). This is a
syntax-shape classifier, not dataflow — a call reached only through an
indirection (a rebound reference, a method passed by name) would not
be caught; no such shape exists in the current file (verified by the
grep above).

## CI wiring

Runs on every push touching this one file (not a repo-wide trigger —
the population is exactly one file, so a path-scoped trigger on that
file alone is the lightest correct choice).

CI: gate
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TARGET = "src/reyn/data/workspace/media_store.py"
_SOLE_CALLER_METHOD = "_unlink_tracked"


def _is_unlink_call(node: ast.AST) -> bool:
    """True for a `Call` node shaped `<expr>.unlink(...)` — any base
    expression, matching `.attr == "unlink"` only (never a text/regex
    match, so a docstring or comment merely naming `.unlink()` cannot
    trigger this)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "unlink"
    )


def find_calls(path: Path) -> "tuple[list[tuple[int, str]], list[int]]":
    """`(violations, sanctioned_linenos)` for every `.unlink(...)` call
    site in *path* — *violations* are `(lineno, enclosing_function_name)`
    pairs whose nearest enclosing function/method is NOT
    `_SOLE_CALLER_METHOD` (module-level top, if any, reports as
    `"<module>"`); *sanctioned_linenos* are the call sites that ARE
    inside it.

    #6050 BLOCKING (lead-coder review): an earlier version of this gate
    returned violations ONLY — if `_SOLE_CALLER_METHOD` itself were
    renamed or deleted, every remaining `.unlink()` call (there would be
    none) trivially satisfies "not inside a DIFFERENT function named
    `_unlink_tracked`", so `violations` comes back empty and the gate
    would claim "OK: _unlink_tracked is the only caller" — true of an
    EMPTY set, false of the invariant this gate exists to state. Callers
    now also check *sanctioned_linenos* is non-empty (the same
    zero-population fail-closed shape `check_faulthandler_timer_api_
    single_caller.py`'s own `if not by_file: return 1` already
    established, and the exact property this gate's own PR review
    flagged as "the class of bug #6050 itself fixes, now in gate form")."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    violations: "list[tuple[int, str]]" = []
    sanctioned: "list[int]" = []

    def walk(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            if _is_unlink_call(child):
                assert isinstance(child, ast.Call)  # narrowed by _is_unlink_call
                if enclosing == _SOLE_CALLER_METHOD:
                    sanctioned.append(child.lineno)
                else:
                    violations.append((child.lineno, enclosing))
            walk(child, enclosing)

    walk(tree, "<module>")
    return sorted(violations), sorted(sanctioned)


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options -- population is one fixed file

    target = _ROOT / _TARGET
    try:
        violations, sanctioned = find_calls(target)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        print(
            f"media-store-unlink-single-caller gate FAILED: could not scan "
            f"{_TARGET} ({exc}). Fails CLOSED on a scan error rather than "
            "silently under-counting.",
            file=sys.stderr,
        )
        return 1

    if not sanctioned:
        print(
            f"media-store-unlink-single-caller gate FAILED: found 0 "
            f".unlink() call(s) inside {_SOLE_CALLER_METHOD}() -- expected "
            "at least 1. Either the sanctioned caller itself was renamed "
            "or removed (update this gate's _SOLE_CALLER_METHOD "
            "deliberately) or the scan itself is broken -- in neither case "
            "is 'no caller found anywhere' the same claim as 'the one "
            "sanctioned caller is the only one', so this fails closed "
            "rather than reporting a vacuous OK.",
            file=sys.stderr,
        )
        return 1

    if violations:
        print(
            "media-store-unlink-single-caller gate FAILED:\n", file=sys.stderr,
        )
        print(
            f"{len(violations)} `.unlink()` call site(s) in {_TARGET} outside "
            f"the one sanctioned caller ({_SOLE_CALLER_METHOD}). The "
            "invariant (#6050) is that ONLY that method may call "
            "Path.unlink() in this file — it fuses deletion with "
            "MediaStore._forget(path) so the tracked-path sets can never "
            "drift from disk again; a second independent unlink call site "
            "would reopen the exact bookkeeping gap #6050 fixed:",
            file=sys.stderr,
        )
        for lineno, enclosing in violations:
            print(f"  {_TARGET}:{lineno}: inside {enclosing}()", file=sys.stderr)
        print(
            f"\nRoute the new delete through self.{_SOLE_CALLER_METHOD}(path) "
            "instead of calling .unlink() directly.",
            file=sys.stderr,
        )
        return 1

    print(
        f"media-store-unlink-single-caller gate OK: {_SOLE_CALLER_METHOD} is "
        f"the only caller of .unlink() in {_TARGET} "
        f"({len(sanctioned)} call site(s), as expected)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
