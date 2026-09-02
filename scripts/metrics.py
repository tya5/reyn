#!/usr/bin/env python3
"""#5631 candidate 1 — measurement gate script (architect requirement).

AST-based, reproducible measurement of the numbers this refactor's own PR
body gate table claims, so a reviewer (or a future session) can re-run it
against any two revisions rather than trust hand-counted numbers.

Usage:
    python scripts/metrics.py <file.py> [--family retry_loop,RecoveryLadder]

Reports, for every function/method definition in the file (optionally
filtered to a comma-separated set of names — a class name includes all its
methods):

  - longest method/function: name, line span, line count
  - flat parameter count of a named top-level function (first positional/
    keyword-only param list, no ``*args``/``**kwargs`` expansion)
  - whole-file line count (``wc -l`` equivalent)
  - new "abstraction" count: number of class definitions in the file
    (informational — the PR body states which one is genuinely new)
  - comment % of the named function's own body (comment lines / total
    non-blank lines in its line span, restricted to full-line ``#``
    comments to match this repo's own comment-policy definition)

Purely reads structure; makes no behavioural claim about the code.
"""
import argparse
import ast
import sys


def function_line_count(node: "ast.FunctionDef | ast.AsyncFunctionDef") -> int:
    return (node.end_lineno or node.lineno) - node.lineno + 1


def flat_param_count(node: "ast.FunctionDef | ast.AsyncFunctionDef") -> int:
    args = node.args
    count = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
    # `self`/`cls` counted like any other flat param, matching how the
    # PR body counts retry_loop's own free-function param list (no self).
    return count


def comment_pct(lines: "list[str]", start: int, end: int) -> float:
    span = lines[start - 1 : end]
    non_blank = [l for l in span if l.strip()]
    comment_lines = [l for l in non_blank if l.strip().startswith("#")]
    if not non_blank:
        return 0.0
    return 100.0 * len(comment_lines) / len(non_blank)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--family", default="", help="comma-separated function/class names to restrict to")
    args = parser.parse_args()

    with open(args.path, "r", encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines()
    tree = ast.parse(src)

    family = {n.strip() for n in args.family.split(",") if n.strip()}

    funcs = []  # (qualname, node)
    classes = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.class_stack: "list[str]" = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            classes.append(node.name)
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def _visit_func(self, node) -> None:
            qual = ".".join(self.class_stack + [node.name])
            funcs.append((qual, node))
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_func(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_func(node)

    Visitor().visit(tree)

    if family:
        def in_family(qual: str) -> bool:
            top = qual.split(".")[0]
            return top in family or qual in family
        funcs = [(q, n) for q, n in funcs if in_family(q)]

    if not funcs:
        print("no matching functions/methods found", file=sys.stderr)
        return 1

    longest_qual, longest_node = max(funcs, key=lambda qn: function_line_count(qn[1]))
    longest_len = function_line_count(longest_node)

    print(f"file: {args.path}")
    print(f"whole-file line count: {len(lines)}")
    print(f"class definitions in file: {len(classes)} ({', '.join(classes) if classes else '-'})")
    print()
    print(f"longest method/function in family {sorted(family) or '(all)'}:")
    print(f"  {longest_qual}  lines {longest_node.lineno}-{longest_node.end_lineno}  ({longest_len} lines)")
    print(f"  comment% of its body: {comment_pct(lines, longest_node.lineno, longest_node.end_lineno):.1f}%")
    print()
    print("flat parameter counts (top-level defs in family, non-nested):")
    for qual, node in sorted(funcs, key=lambda qn: -function_line_count(qn[1])):
        print(f"  {qual}: {flat_param_count(node)} params, {function_line_count(node)} lines")

    return 0


if __name__ == "__main__":
    sys.exit(main())
