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
  - whole-file line-count breakdown: blank / ``#`` comment / docstring /
    code, so a "the file grew" claim can be attributed to a specific
    category rather than asserted from the total alone. A docstring line
    is any line inside a module/class/function's own first-statement
    string-literal span (found via ``ast``); blank/comment are checked
    on every other line; everything left over is code.

Purely reads structure; makes no behavioural claim about the code.
"""
import argparse
import ast
import sys


def classify_lines(lines: "list[str]", tree: "ast.Module") -> "dict[str, int]":
    docstring_lines: "set[int]" = set()

    def mark_docstring(node) -> None:
        body = getattr(node, "body", None)
        if not body:
            return
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            for ln in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                docstring_lines.add(ln)

    mark_docstring(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            mark_docstring(node)

    counts = {"blank": 0, "comment": 0, "docstring": 0, "code": 0}
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if i in docstring_lines:
            counts["docstring"] += 1
        elif stripped == "":
            counts["blank"] += 1
        elif stripped.startswith("#"):
            counts["comment"] += 1
        else:
            counts["code"] += 1
    return counts


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

    breakdown = classify_lines(lines, tree)
    print(f"file: {args.path}")
    print(f"whole-file line count: {len(lines)}")
    print(
        f"  breakdown: comment={breakdown['comment']} docstring={breakdown['docstring']} "
        f"blank={breakdown['blank']} code={breakdown['code']}"
    )
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
