"""#3995/#4002 — resolving a ``__file__``-rooted expression to a real Path.

Two gates each build their own boolean check on top of this ONE shared
resolver — they do NOT share a single predicate (that was tried, twice,
and retracted twice: #3995's original "leaves own directory by hop count"
missed #4002's ``Path(__file__).parent / "_support"`` counter-example, and
the follow-up "does the target travel with the file" idea was itself
retracted by lead-coder/architect as UNDECIDABLE from source text alone —
"safety of ``.parent / X`` is a property of the MOVE, not of the source
text"):

- ``scripts/check_migration_diff_shape.py`` (move-time, "a′"): a REAL move
  already happened (the file sits at its new path on disk, in CI's real
  checkout). This gate does not need to guess anything — it resolves each
  expression using the file's ACTUAL new location and asks the ground-truth
  question, "does the target still exist?"
- ``scripts/check_file_depth_reference.py`` (add/static-time, "b"): no move
  is happening (a brand-new or otherwise-untouched file), so there is
  nothing to re-resolve against — only a narrow, FS-derived STATIC proxy is
  possible: does the expression reach ``tests/`` itself (or above it), or
  land on one of ``tests/``'s CURRENT direct child directories? This is a
  reintroduction deterrent, not a substitute for a′'s ground-truth check.

## Why a bare ``__file__``, never ``x.__file__``

``Path(reyn.__file__)`` — an imported PACKAGE's own ``__file__`` attribute
— is syntactically an ``ast.Attribute`` node (``value=Name('reyn'),
attr='__file__'``), never a bare ``ast.Name(id='__file__')``. This
module's root-detection only ever matches the bare ``Name`` form, so a
package's own ``__file__`` is excluded BY CONSTRUCTION — the false
positive tui-coder's #3995 measurement found in
``test_codeact_runner_1593.py``: ``Path(reyn.__file__)...`` locates the
INSTALLED PACKAGE's src tree, unrelated to where the TEST FILE sits.

## Why AST, not regex

A regex enumerates SYNTACTIC FORMS, and #3990's own history is the
counter-example: its regex caught ``Path(__file__).parent.parent`` but
missed ``Path(__file__).resolve().parent.parent`` (#3994). This module
instead evaluates the parsed expression tree structurally — a new spelling
of the same idea (``.parent`` chaining / ``.parents[N]`` / ``/ ".."`` /
nested ``os.path.dirname``) resolves correctly by construction, not by
having been anticipated.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _const_int(node: "ast.expr | None") -> "int | None":
    """The literal int a subscript index evaluates to, or None if it is not
    a plain integer constant (a dynamic index cannot be resolved
    statically — treated the same as any other unrecognised shape)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    return None


def _resolve(node: "ast.expr", anchor: Path) -> "Path | None":
    """The real filesystem :class:`Path` ``node`` denotes, if ``node`` is
    (a subexpression of) an expression rooted in a bare ``__file__`` name —
    treating *anchor* as ``__file__``'s value — else ``None`` (not a
    ``__file__``-rooted expression at all: an unrelated variable,
    ``pkg.__file__``, or a shape this resolver doesn't recognise)."""
    if isinstance(node, ast.Name) and node.id == "__file__":
        return anchor

    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, anchor)
        if base is None:
            return None
        if node.attr == "parent":
            return base.parent
        if node.attr in ("resolve", "absolute"):
            return base
        # `.parents` alone (no subscript) is handled by the Subscript case
        # below; any OTHER attribute (`.name`, `.stem`, ...) breaks the
        # chain — no longer a directory-reference expression.
        return None

    if isinstance(node, ast.Subscript):
        target = node.value
        if isinstance(target, ast.Attribute) and target.attr == "parents":
            base = _resolve(target.value, anchor)
            if base is None:
                return None
            n = _const_int(node.slice)
            if n is None:
                return None
            result = base
            for _ in range(n + 1):  # parents[0] == .parent; parents[N] N+1 hops
                result = result.parent
            return result
        return None

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _resolve(node.left, anchor)
        if base is None:
            return None
        if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
            return base / node.right.value
        return None

    if isinstance(node, ast.Call):
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if func_name == "resolve" and isinstance(node.func, ast.Attribute):
            return _resolve(node.func.value, anchor)
        if func_name in ("Path", "abspath", "normpath") and len(node.args) == 1:
            return _resolve(node.args[0], anchor)
        if func_name == "dirname" and len(node.args) == 1:
            base = _resolve(node.args[0], anchor)
            return None if base is None else base.parent
        return None

    return None


def parse_file_relative_targets(source: str, anchor: Path) -> "list[Path]":
    """Every distinct real :class:`Path` a ``__file__``-rooted expression in
    *source* denotes, treating *anchor* as the value of ``__file__`` —
    e.g. for ``anchor=tests/hooks/test_x.py``, ``Path(__file__).parent``
    resolves to ``tests/hooks``. Empty for a syntax error (not this
    resolver's problem to flag).

    Only MAXIMAL chains are returned — an intermediate sub-expression
    (``Path(__file__).parent`` inside the larger ``Path(__file__).parent /
    "x"``) is suppressed when its own immediate parent ALSO resolves,
    since ``ast.walk`` visits every node in a chain independently and
    would otherwise report the file's own directory as if it were a
    separate, standalone usage. This matters concretely: for a file living
    ONE level under *anchor*'s intended ``tests/`` root, ``Path(__file__).
    parent`` (the file's own directory) is ITSELF a direct child of
    ``tests/`` — without this suppression, `check_file_depth_reference.py`
    would flag every such file's `.parent` reference as if it named a
    fixed ``tests/``-root peer, even when the chain goes no further than
    the file's own directory."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    parents: "dict[ast.AST, ast.AST]" = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    seen: "dict[Path, None]" = {}  # insertion-ordered dedup
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Attribute, ast.Subscript, ast.BinOp, ast.Call)):
            continue
        target = _resolve(node, anchor)
        if target is None:
            continue
        parent = parents.get(node)
        if parent is not None and _resolve(parent, anchor) is not None:
            continue  # a sub-expression of a larger resolvable chain
        seen[target] = None
    return list(seen)


def _module_level_nodes(tree: ast.Module) -> "list[ast.AST]":
    """Every node reachable from *tree* WITHOUT descending into a
    ``def``/``async def``/``class`` body — the "runs at import time" subset
    of the module. A directory a test creates at runtime (a fixture's
    ``tmp_path``-scoped output dir, a `TemporaryDirectory`, ...) is, by
    construction, always built inside a function body — never at bare
    module scope, since nothing has run yet at import time. Restricting to
    this subset is what lets :func:`module_level_glob_roots` require
    existence without false-positiving on that whole (very common)
    pattern."""
    nodes: "list[ast.AST]" = []

    def _walk(node: ast.AST) -> None:
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            _walk(child)

    _walk(tree)
    return nodes


def module_level_glob_roots(source: str, anchor: Path) -> "list[Path]":
    """Every ``__file__``-rooted directory that *source* uses, AT MODULE
    LEVEL (import time, never inside a function/class body), as the base
    of a ``.glob(...)``/``.rglob(...)`` call — the "eager fixture
    discovery" pattern (``_FIXTURES_ROOT = Path(__file__).parent /
    "fixtures"; _FIXTURE_FILES = sorted(_FIXTURES_ROOT.rglob("*.jsonl"))``)
    that #4019's real instance (``tests/dev/test_replay_fixture_no_
    stacking_3634.py``) broke: the directory silently didn't exist, the
    glob silently returned nothing, and the test's own parametrization
    silently collected zero cases instead of failing.

    Deliberately narrow, matching the SAME "module-level only" reasoning
    :func:`_module_level_nodes` documents: a target used this way, this
    early, is a genuine "this SHOULD already exist on disk" claim, not a
    runtime-created output directory (those live inside function bodies).
    Empty for a syntax error."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    module_nodes = _module_level_nodes(tree)
    bindings: "dict[str, Path]" = {}
    for node in module_nodes:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target_path = _resolve(node.value, anchor)
            if target_path is not None:
                bindings[node.targets[0].id] = target_path

    roots: "dict[Path, None]" = {}
    for node in module_nodes:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("glob", "rglob"):
            continue
        base = node.func.value
        # Directly `Path(__file__)...glob(...)` (the target resolves without
        # an intermediate name binding).
        direct = _resolve(base, anchor)
        if direct is not None:
            roots[direct] = None
            continue
        # `_SOME_NAME.glob(...)` where `_SOME_NAME` was bound, at module
        # level, to a __file__-rooted expression above.
        if isinstance(base, ast.Name) and base.id in bindings:
            roots[bindings[base.id]] = None
    return list(roots)
