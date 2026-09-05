#!/usr/bin/env python3
"""#5455 ②: a static gate — every call to
``reyn.config.loader._load_yaml`` passes a ``vocabulary=`` keyword
argument that is neither omitted nor a bare ``None``.

## The class this closes

``_load_yaml(path, *, vocabulary)`` has no default for ``vocabulary`` —
omitting it is a ``TypeError`` at the call site, so it already cannot be
imported into a NEW file that forgets to decide. But ``_load_yaml`` is a
module-private helper: nothing stops a future refactor from giving it a
default again "for convenience", silently reopening the exact #4501/#4515
hole this issue exists to close (a new operator-editable yaml file ships
with no unknown-key disclosure, and nothing says so). This gate pins the
CALL-SITE shape directly — a `vocabulary=` keyword argument present at
every `_load_yaml(` call — so a regression on the SIGNATURE side (the
default quietly coming back) still shows up here even though it would no
longer be a Python-level TypeError.

Also rejects a LITERAL ``None`` (architect BLOCKING finding on this PR's
first revision): ``vocabulary`` accepts either a callable or one of
``_CheckedElsewhere``'s named members (``CHECKED_BY_CONFIG_VALIDATE`` /
``CHECKED_AT_LOAD_POINT`` / ``CHECKED_BY_CALLER``) — never ``None``,
which would collapse those three distinct, reviewable claims into one
value indistinguishable from "nobody decided". A future contributor
copying a neighboring ``vocabulary=None`` for a genuinely new file would
reopen the exact hole this issue closes while this gate stayed green (a
bare presence check does not see WHAT was passed, only THAT something
was) — see ``_CheckedElsewhere``'s own docstring (``reyn.config.loader``)
for the full reasoning.

## Why AST, not a signature-default check

Checking "does `_load_yaml` still lack a default" answers only half the
question — it says nothing about whether some caller has silently reached
in via `_load_yaml(path)` after a hypothetical future refactor. Statically
finding every call site and requiring the keyword there is the same
"witness the actual failure mode, not a stand-in for it" discipline the
`vocabulary` parameter's own docstring names as this issue's driving
principle: the failure is a NEW CALL SITE deciding nothing, not a NEW
DEFAULT VALUE existing.

## Real source is the reader

This gate parses the real `src/` tree and reports the real line/file of
any offending call — nothing here is a fixture; a strip (deleting a
`vocabulary=` kwarg from any current call site) turns this RED against
the actual codebase.

## #5801: ``token_map`` gets the SAME treatment, as a second axis

``_load_yaml`` grew a second required keyword, ``token_map`` (#5801 —
reyn's own ``${REYN_*}`` token expansion, previously applied by hand
after the fact to the merged config only, never to ``profile.yaml``
at all — the real incident this closes). Same reasoning, same "AST
call-site gate outlives a signature regression" argument, kept as a
SEPARATE visitor/function (:func:`find_token_map_violations`) rather
than folded into :func:`find_violations` above so the two axes stay
independently testable — a fixture exercising only the `vocabulary=`
axis (the tests below) does not also have to supply a real
`token_map=` to stay a valid accept-case, and vice versa. `main()`
below reports both."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"


class _CallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: "list[tuple[int, str]]" = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 (ast API name)
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if name == "_load_yaml":
            vocab_kw = next((kw for kw in node.keywords if kw.arg == "vocabulary"), None)
            if vocab_kw is None:
                self.violations.append((node.lineno, ast.dump(node)[:120]))
            elif isinstance(vocab_kw.value, ast.Constant) and vocab_kw.value.value is None:
                # #5455 ②, architect BLOCKING finding: a bare `None` is no
                # longer a valid vocabulary= value at all (see
                # _CheckedElsewhere's own docstring, reyn.config.loader) —
                # it collapses 3 distinct reasons into one value nothing
                # can tell apart. Flag it exactly like a missing kwarg.
                self.violations.append((node.lineno, ast.dump(node)[:120]))
        self.generic_visit(node)


def find_violations(src_dir: Path = _SRC) -> "list[tuple[Path, int]]":
    """Every ``_load_yaml(...)`` call under *src_dir* missing a
    ``vocabulary=`` keyword argument — the definition site itself
    (``def _load_yaml``) is not a call and is never matched. *src_dir*
    defaults to this repo's real ``src/`` tree; a test overrides it with
    a synthetic ``tmp_path`` tree."""
    violations: "list[tuple[Path, int]]" = []
    for path in sorted(src_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        visitor = _CallVisitor()
        visitor.visit(tree)
        for lineno, _ in visitor.violations:
            violations.append((path, lineno))
    return violations


class _TokenMapCallVisitor(ast.NodeVisitor):
    """#5801: the ``token_map=`` twin of :class:`_CallVisitor` above —
    same shape, a DIFFERENT required keyword. A bare ``None`` is flagged
    too (an omitted-expansion face would otherwise read exactly like a
    genuinely-empty ``{}`` map, and nothing downstream could tell "this
    face has no reyn tokens" from "nobody decided")."""

    def __init__(self) -> None:
        self.violations: "list[tuple[int, str]]" = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 (ast API name)
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if name == "_load_yaml":
            kw = next((kw for kw in node.keywords if kw.arg == "token_map"), None)
            if kw is None:
                self.violations.append((node.lineno, ast.dump(node)[:120]))
            elif isinstance(kw.value, ast.Constant) and kw.value.value is None:
                self.violations.append((node.lineno, ast.dump(node)[:120]))
        self.generic_visit(node)


def find_token_map_violations(src_dir: Path = _SRC) -> "list[tuple[Path, int]]":
    """#5801: every ``_load_yaml(...)`` call under *src_dir* missing a
    ``token_map=`` keyword argument, or passing a bare ``None`` — the
    structural gate that makes "a new reyn-token-aware yaml face reads
    a file but never expands it" (the real #5801 defect: profile.yaml's
    ``context_path``) impossible to write for any face going through
    this function, not just impossible to remember to check."""
    violations: "list[tuple[Path, int]]" = []
    for path in sorted(src_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        visitor = _TokenMapCallVisitor()
        visitor.visit(tree)
        for lineno, _ in visitor.violations:
            violations.append((path, lineno))
    return violations


def main() -> int:
    vocab_violations = find_violations()
    token_map_violations = find_token_map_violations()
    if not vocab_violations and not token_map_violations:
        print(
            "OK: every _load_yaml(...) call passes vocabulary= and "
            "token_map= with a real value (never a bare None)."
        )
        return 0
    if vocab_violations:
        print("load-yaml-vocabulary gate FAILED:\n")
        print(
            f"{len(vocab_violations)} call(s) to _load_yaml(...) either omit the "
            f"required vocabulary= keyword argument, or pass a bare None "
            f"(no longer valid — see below):\n"
        )
        for path, lineno in vocab_violations:
            print(f"  {path.relative_to(_REPO_ROOT)}:{lineno}")
        print(
            "\nPass vocabulary=<unknown_config_keys-shaped callable> to WARN on "
            "this file's own unknown keys at read time, or one of "
            "_CheckedElsewhere's named members (CHECKED_BY_CONFIG_VALIDATE / "
            "CHECKED_AT_LOAD_POINT / CHECKED_BY_CALLER) if this file's content "
            "is validated elsewhere — never a bare None, which collapses all "
            "three of those distinct reasons into one value nothing can tell "
            "apart. See _load_yaml's and _CheckedElsewhere's own docstrings "
            "(src/reyn/config/loader.py).\n"
        )
    if token_map_violations:
        print("load-yaml-token-map gate FAILED:\n")
        print(
            f"{len(token_map_violations)} call(s) to _load_yaml(...) either omit "
            f"the required token_map= keyword argument, or pass a bare None "
            f"(#5801 — see below):\n"
        )
        for path, lineno in token_map_violations:
            print(f"  {path.relative_to(_REPO_ROOT)}:{lineno}")
        print(
            "\nPass token_map={\"REYN_PROJECT_DIR\": ...} (plus REYN_AGENT_NAME "
            "when this face has an agent identity) — an explicit {} only for a "
            "face this issue's own scoping decided has genuinely no reyn-token "
            "value to offer. See _load_yaml's own docstring and "
            "reyn.plugins.tokens.expand_yaml_tokens_or_refuse "
            "(src/reyn/config/loader.py, src/reyn/plugins/tokens.py)."
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
