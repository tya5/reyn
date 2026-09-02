#!/usr/bin/env python3
"""#5631: measure the shape facts a structural-refactor PR is gated on.

The gate is RELATIVE, not absolute (architect, #5631): a PR states the numbers
at its own merge-base and the numbers at its head, and the gate reads the pair.
Absolute thresholds baked into an issue body go stale the moment anything else
lands — that is exactly how #5631's first gate ended up quoting a baseline
measured before #5618 (``_run_with_shrink`` 448/7, when the merge-base by then
said 457/8), which no refactor could have satisfied because the difference was
another PR's deliberate addition. So: measure both ends, with the same script,
and ship the script.

Reports per function, for the ones named on the command line (or every
function when none are named):

- ``span``     — end_lineno - lineno + 1, the same "how long is this function"
                 the issue's own table uses.
- ``closures`` — nested ``def``/``async def``. ``lambda`` counts too: the
                 refactor's claim is "no closure captures the enclosing scope
                 any more", and rewriting a nested ``def`` as a ``lambda``
                 would satisfy a def-only count while changing nothing.
- ``self_attrs`` — distinct ``self.X`` names the function touches, the issue's
                 "``self.*`` 種類" (a coupling measure: it should stay the SAME
                 across a pure extraction, not drop — a drop means behaviour
                 moved out of the class, not that coupling improved).
- ``max_flat_params`` — the largest positional+keyword arity among the function
                 and anything nested in it, excluding ``self``. #5631's own
                 escape hatch: a Parameter Object is introduced only if this
                 exceeds 6 after extraction.

Usage:
    python scripts/function_shape_metrics.py <file> [function ...]
    python scripts/function_shape_metrics.py <file> --json

Reads only the file given. No git, no network: to measure a merge-base, check
it out (or ``git show <sha>:<path> > /tmp/x.py``) and run this on that copy.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

#: The three node types that introduce a new scope and can capture from an
#: enclosing one. ``ast.AST`` is too wide to index ``.args`` on.
_Callable = "ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda"


def _nested_callables(node: ast.AST) -> "list[_Callable]":
    """Every callable syntactically inside ``node``, excluding ``node``."""
    out = []
    for child in ast.walk(node):
        if child is node:
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            out.append(child)
    return out


def _arity(fn: "_Callable") -> int:
    """Positional + keyword-only params, not counting ``self``."""
    args = fn.args
    names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
    return len([n for n in names if n != "self"])


def measure(path: Path, wanted: "set[str] | None") -> dict:
    tree = ast.parse(path.read_text())
    rows = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if wanted is not None and node.name not in wanted:
            continue
        nested = _nested_callables(node)
        attrs = {
            n.attr
            for n in ast.walk(node)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id == "self"
        }
        end = node.end_lineno or node.lineno
        rows[node.name] = {
            "span": end - node.lineno + 1,
            "closures": len(nested),
            "self_attrs": len(attrs),
            "self_attr_names": sorted(attrs),
            "max_flat_params": max([_arity(node)] + [_arity(f) for f in nested]),
        }
    return {"file": str(path), "file_lines": len(path.read_text().splitlines()), "functions": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("functions", nargs="*", help="function names; default: all")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args()

    if not ns.file.exists():
        print(f"no such file: {ns.file}", file=sys.stderr)
        return 2

    result = measure(ns.file, set(ns.functions) or None)
    if ns.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    print(f"{result['file']}  ({result['file_lines']} lines)")
    if not result["functions"]:
        print("  (no matching function)")
        return 1
    for name, row in sorted(result["functions"].items()):
        print(
            f"  {name}: span={row['span']} closures={row['closures']} "
            f"self_attrs={row['self_attrs']} max_flat_params={row['max_flat_params']}"
        )
        print(f"      self.*: {', '.join(row['self_attr_names']) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
