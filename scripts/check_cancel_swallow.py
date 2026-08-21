#!/usr/bin/env python3
"""#4988 — a static gate against the "cancel-then-await-then-swallow"
class: a coroutine cancels a task/future it owns, awaits it, and catches
the resulting ``CancelledError`` unconditionally — with no check for
whether the CATCHING coroutine's own task was ALSO, independently,
externally cancelled at that exact same await.

## The failure this closes

    some_task.cancel()
    try:
        await some_task
    except asyncio.CancelledError:
        pass          # <-- swallows BOTH cases below, indistinguishably

``await some_task`` raises ``CancelledError`` for two different reasons
that are, at the `except` clause, indistinguishable by the exception
alone:

  (a) ``some_task``'s own cancellation outcome — the ``.cancel()`` call
      two lines up, exactly what this pattern exists to absorb.
  (b) the CURRENT coroutine's own task being independently, externally
      cancelled (e.g. a shutdown sweep, `asyncio.run()`'s / pytest-
      asyncio's own end-of-loop `_cancel_all_tasks`, which cancels every
      task in `asyncio.all_tasks()` with no ordering guarantee) while
      suspended at this exact await.

An unconditional ``pass`` treats both the same: case (b) is silently
absorbed, and the enclosing function returns NORMALLY instead of letting
a genuine external cancellation propagate — the caller's own shutdown/
teardown appears to have completed when it did not. Found in
``events.py``'s ``drain()``/``stop_dispatch()`` (#4986/#4988) and,
census'd from there, 4 more sites with the identical shape.

## The fix this gate accepts (already applied at every site above)

Python 3.11+ (this repo's own floor — ``pyproject.toml``'s
``requires-python = ">=3.11"``) added ``Task.cancelling()`` for exactly
this: the number of pending cancellation requests against the CURRENT
task. Checking it in the ``except`` handler tells (a) apart from (b):

    some_task.cancel()
    try:
        await some_task
    except asyncio.CancelledError:
        if asyncio.current_task().cancelling() > 0:
            raise

reyn already had this exact pattern before #4988 — ``session.py``'s own
#3377 precedent (``_driver.cancelling() > 0``) — this gate's fix mirrors
it, not a new invention. Python's own docs (asyncio-task.html): "Should
the coroutine nevertheless decide to suppress the cancellation, it needs
to call Task.uncancel() in addition to catching the exception" —
checking ``cancelling()`` before deciding to swallow is the read-side of
that same discipline.

## Scope and known gap (disclosed, not silently narrowed)

This gate is a STRUCTURAL pattern match (AST), not a semantic proof: it
flags a `try: await <name> / except CancelledError:` handler with no
`raise` anywhere in it, PRECEDED in the same function by `<name>.cancel()`
on that exact name, and no `.cancelling()` call anywhere in the handler.
A site that legitimately does not need the check (e.g. it can prove by
construction that its own task is never independently cancellable at
that point) has no way to declare that here — the only escape is to
restructure the code so the pattern's own shape (cancel-a-name, await
that SAME name, catch-and-swallow) doesn't match, same as
`check_collect_events_settle.py`'s own precedent for this repo's static
gates. A hand-rolled indirection (cancelling through a wrapper function
whose name this gate can't trace back to the literal `.cancel()` call)
is a disclosed blind spot, not a claim of exhaustive coverage — mirrors
that same gate's own "not every hand-rolled shape is traceable" caveat.

This gate only scans ``src/`` (production code) — the failure mode is a
production defect (a real shutdown silently appearing to complete), not
a test-authoring one.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC_DIR = _ROOT / "src" / "reyn"


def _is_cancelled_error_handler(handler: ast.ExceptHandler) -> bool:
    """True if *handler* catches ``asyncio.CancelledError`` or a bare
    ``CancelledError`` (either import shape this codebase uses)."""
    node = handler.type
    if node is None:
        return False
    names = [node] if not isinstance(node, ast.Tuple) else list(node.elts)
    for n in names:
        if isinstance(n, ast.Attribute) and n.attr == "CancelledError":
            return True
        if isinstance(n, ast.Name) and n.id == "CancelledError":
            return True
    return False


def _handler_has_raise(handler: ast.ExceptHandler) -> bool:
    """True if any ``raise`` statement (bare or with an exception)
    appears anywhere in *handler*'s own body — the handler already
    propagates, at least on some path, so it is not a silent swallow."""
    return any(isinstance(n, ast.Raise) for stmt in handler.body for n in ast.walk(stmt))


def _handler_checks_cancelling(handler: ast.ExceptHandler) -> bool:
    """True if *handler* calls ``.cancelling()`` anywhere in its own
    body — the discriminator this gate exists to require (session.py's
    own #3377 precedent; see this module's own docstring)."""
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "cancelling"
        for stmt in handler.body
        for n in ast.walk(stmt)
    )


def _awaited_name(try_node: ast.Try) -> "str | None":
    """The bare name being awaited, if *try_node*'s body is (or starts
    with) a single ``await <Name>`` — the shape every known instance of
    this class uses (``await task`` / ``await join_future`` / ``await
    self._drain_task``, never a compound expression). Returns the
    dotted-attribute path as a string (e.g. ``self._drain_task``) or a
    bare name, so it can be compared against a preceding ``<same>.cancel()``
    call textually."""
    for stmt in try_node.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await):
            awaited = stmt.value.value
        elif isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Await):
            awaited = stmt.value.value
        else:
            continue
        return _dotted_name(awaited)
    return None


def _dotted_name(node: ast.AST) -> "str | None":
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base is not None else None
    return None


def _has_preceding_cancel_call(func_body_root: ast.AST, try_node: ast.Try, name: str) -> bool:
    """True if ``<name>.cancel()`` is called anywhere in *func_body_root*
    at a line number BEFORE *try_node*'s own line — the "this code owns
    and just told this task to cancel" signal that makes the awaited
    name's own cancellation the EXPECTED outcome (case (a) in this
    module's own docstring)."""
    for node in ast.walk(func_body_root):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "cancel"
            and _dotted_name(node.func.value) == name
            and node.lineno < try_node.lineno
        ):
            return True
    return False


def _check_function(func: "ast.FunctionDef | ast.AsyncFunctionDef") -> "list[tuple[int, str]]":
    """Return ``[(lineno, awaited_name), ...]`` for every cancel-then-
    await-then-swallow site found in *func*'s own body."""
    hits: "list[tuple[int, str]]" = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Try):
            continue
        name = _awaited_name(node)
        if name is None:
            continue
        for handler in node.handlers:
            if not _is_cancelled_error_handler(handler):
                continue
            if _handler_has_raise(handler):
                continue  # propagates on at least one path — not a swallow
            if _handler_checks_cancelling(handler):
                continue  # already discriminates — the accepted fix shape
            if not _has_preceding_cancel_call(func, node, name):
                continue  # not "this code's own cancel" — different shape
            hits.append((node.lineno, name))
    return hits


def offending_files(src_dir: Path) -> "list[tuple[Path, list[tuple[int, str]]]]":
    """The gate's whole decision, isolated from CLI/printing — directly
    testable, mirrors ``check_collect_events_settle.py``'s own split."""
    offenders: "list[tuple[Path, list[tuple[int, str]]]]" = []
    for path in sorted(src_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        hits: "list[tuple[int, str]]" = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                hits.extend(_check_function(node))
        if hits:
            offenders.append((path, sorted(hits)))
    return offenders


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options — a whole-tree scan against a baseline of zero
    offenders = offending_files(_SRC_DIR)

    if not offenders:
        print(
            "OK: no cancel-then-await-then-swallow site found without a "
            "Task.cancelling() check in the except handler."
        )
        return 0

    print("cancel-swallow gate FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} file(s) cancel a task/future they own, await "
        "it, and catch the resulting CancelledError unconditionally "
        "(#4986/#4988) — this cannot tell 'that task's own cancellation' "
        "apart from 'my OWN task was independently, externally cancelled "
        "at this same await', so the second case is silently absorbed "
        "instead of propagating:",
        file=sys.stderr,
    )
    for path, hits in offenders:
        rel = path.relative_to(_ROOT)
        for line, name in hits:
            print(f"  {rel}:{line}: cancel-then-await-swallow of {name!r}", file=sys.stderr)

    print(
        "\nFix: check `asyncio.current_task().cancelling() > 0` in the "
        "except handler and `raise` when true (session.py's own #3377 "
        "precedent; see events.py's drain()/stop_dispatch() for the "
        "worked example, #4988).\n"
        "\nThis gate's own starting population is zero, so any hit here "
        "is a new regression, not inherited debt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
