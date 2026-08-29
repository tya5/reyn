#!/usr/bin/env python3
"""#4965/#4966 — a static gate against the collect_events-without-settle
silent-pass class.

## The failure this closes

``tests/_support/events.py::collect_events(log)`` returns a plain list a
real subscriber appends to. Since #4961 C, ``EventLog.emit()`` dispatches
to subscribers on a background consumer task whenever a running event loop
exists (inline, synchronously, only when NO loop is running — see
``events.py``'s own ``emit()`` docstring). A test that reads the collected
list right after the triggering ``await``/``asyncio.run(...)``, with
nothing that yields control in between, can race the consumer and see a
stale/incomplete list — fixed by awaiting ``tests._support.events.settle(log)``
(or ``log.drain()`` directly) immediately before the read.

31 real instances of exactly this were found in the SAME investigation that
produced this gate, and NONE of them showed up as a CI failure: every one
of the 31 asserts an ABSENCE (``assert not any(...)``, ``assert [...] ==
[]``) — an event that never arrived (because the read raced the consumer)
satisfies "absence" exactly as well as an event that genuinely never fires.
The 9(+1) sites #4966 originally found and fixed all asserted PRESENCE, so
a missed delivery showed up as a red assert — CI found those on its own.
This absence-asserting half cannot be found by running the suite and
waiting for red: it passes control-flow-unstable-but-green tonight, next
week, and every CI run after that, for the same reason a dead permission
gate (#3037) passes — the code path that would fail is never actually
exercised by the assertion. Per CLAUDE.md: "If CI can catch the violation,
write the gate, not a rule here" — this half of the class specifically
CANNOT be caught by CI running the suite, so it needs a static gate.

## Why NOT grep for the literal name ``collected``

The 31 instances were found by an AST-aware audit, not a grep for
``collected`` — the actual variable names bound to a derived read spanned
``blocked`` / ``denial_events`` / ``started`` / ``emitted`` /
``skip_events`` / ``matching`` / ``new_events`` / ``parent_events`` /
``unrecovered`` and more. A grep-shaped gate matching only the literal
string ``collected`` would have missed the majority of the real population
— exactly the mistake the original 9(+1)-file census made (filtered on "no
loop used" instead of "no settle before a sync read", and MISSED sites
that DID use a loop but still read synchronously). This gate instead
traces the assignment chain: any name bound (directly or via one more
level of derivation — a comprehension/filter/index/unpack over an already-
tracked name) from a ``collect_events(...)`` call is tracked, and every
LOAD of a tracked name is checked.

## Why NOT search by helper-function NAME either (a second application of
## the same lesson, found the SAME night)

A CI run surfaced 40 more failures using a DIFFERENT collection mechanism
entirely: a hand-rolled ``session.subscribe_audit_events(lst.append)`` or
``log.add_subscriber(lambda e: lst.append(e))`` — never touching
``collect_events()`` at all, so invisible to everything above. The
tempting fix — search for helper functions NAMED like ``collect_events``
or ``_EventSink`` — repeats the exact mistake: a real site used a local
helper named ``_collect_events`` (one leading underscore different) that
called ``add_subscriber`` directly, and would stay invisible to any
NAME-based search forever. The only discriminator that closes the
population BY CONSTRUCTION (lead-coder finding): there are exactly two
ways anything becomes an ``EventLog`` subscriber — ``add_subscriber()``
(the sole ``self._subscribers.append`` call site, ``events.py``) and the
``EventLog(subscribers=[...])`` constructor argument. So this gate also
tracks the list bound via ``<x>.add_subscriber(<name>.append)`` or
``<x>.subscribe_audit_events(lambda e: <name>.append(e))`` — the two
statically-resolvable shapes of "did this call register a subscriber",
not "does this look like a collector." A hand-rolled Sink CLASS instance
(``class XSink: def __call__(self, e): self.events.append(e)``) is NOT
statically resolvable to a single tracked name this way and is a
disclosed gap, not covered — see ``_tracked_name_from_subscriber_arg``'s
own docstring.

## Why this gate only checks ``async def`` functions (③ closed at the
## mechanism level, not the test level)

The original investigation split the 31 sites into three groups: ①
live ``async def`` tests reading synchronously (14 — needs
``await settle(log)`` before the read), ② fully-synchronous tests with
no event loop anywhere (4 — never in scope; ``emit()``'s own no-loop
branch dispatches inline, always has, needs nothing), and ③
``asyncio.run(coro)``-wrapped tests reading AFTER ``asyncio.run()``
returns (15+1 — the loop has already closed by then, so no ``await`` can
even be written at the read site).

③ turned out to be a mechanism gap, not a test-authoring gap: ``asyncio.
run()``'s own teardown cancels every still-running task (including this
EventLog's background consumer) before the wrapped coroutine's own task
is considered fully done. #4966 closed this in ``_dispatch_consumer``
itself — on ``CancelledError``, whatever remains queued is flushed
SYNCHRONOUSLY before the cancellation is allowed to propagate, so by the
time ``asyncio.run()`` returns control to its caller, full delivery is
already guaranteed by construction. Measured directly (a synthetic probe
against both the pre- and post-fix consumer): every event was delivered
by the time ``asyncio.run()`` returned in every combination tried. A
plain ``def`` function is therefore NEVER flagged by this gate, even if
it wraps ``asyncio.run(...)`` and reads a tracked name afterward — ③
needs zero test changes.

An inner ``async def`` (e.g. a ``_it()``/``_run_and_settle()``-style
wrapper passed TO ``asyncio.run()``) is unaffected by this narrowing —
it's its own ``ast.AsyncFunctionDef`` node, checked independently by the
same rule as any other ① site: the mechanism protects delivery at
CANCELLATION time, not against a read racing the consumer while that
inner coroutine is still normally executing and hasn't returned yet.

## What counts as "safe to read after" (a yield point)

- ``await settle(...)`` / ``await <x>.drain()`` — the explicit fix.
- ``await asyncio.sleep(...)`` SPECIFICALLY — a real yield; used by a
  small number of hand-rolled polling loops (``while cond: await
  asyncio.sleep(N)``) that predate ``settle()`` and are equally safe
  (polling yields control repeatedly, giving the consumer a chance to
  run, same as ``_wait_for``). Matched by attribute name AND receiver
  (``asyncio.sleep``, not any object's own ``.sleep()`` method) — a
  receiver-less attribute match would be too permissive.
- ``await <name>(...)`` where *name* is in the CLOSED
  ``_POLLING_HELPER_NAMES`` set (``_wait_for``, ``_wait_for_event``,
  ``_wait_for_file``, ``_wait_for_calls``, ``_wait_until``,
  ``wait_until``, ``_poll``) — the polling helpers actually in use across
  this suite, enumerated explicitly. NOT a substring match on "wait":
  lead-coder measured directly that ``"wait" in name.lower()`` — an
  earlier draft of this gate — silently accepted every bare-name async
  helper whose name merely contains "await" as a substring (``"await"``
  itself contains ``"wait"`` starting at its second character —
  ``_await_reply``, ``_await_scheduled_source_build``, and more all
  matched, none of them polling helpers), and would separately have
  accepted ``<x>.wait()`` (e.g. ``asyncio.Event.wait()``) had it ever hit
  the Name-callee branch — ``Event.wait()`` does NOT reliably yield
  (CPython returns immediately, without ever yielding, if the event is
  already set). A wrong ACCEPT here is exactly the silent-pass class this
  gate exists to close, so the set is closed, not fuzzy-matched
  (mirrors ``bounding.py``'s ``BOUNDING_KEYS``/``UnknownBoundingKeyError``
  shape) — an unrecognized await name is never treated as a yield point.
- A tracked-name LOAD that occurs inside a ``lambda`` is exempt outright,
  regardless of yield points in the enclosing function — a lambda body is
  always a PREDICATE passed to some caller (invariably one of the above
  polling helpers in this codebase's own idiom), not a direct read; the
  polling LOOP that calls it, not the predicate itself, is what actually
  yields.
- A tracked-name LOAD inside a DIFFERENT function than the one containing
  its ``collect_events(...)`` binding is exempt (the gate only tracks
  names within a single function's own body — a name imported into another
  scope, e.g. via closure or a helper's own parameter, is out of this
  gate's tracked-name graph, not flagged as either safe or broken; the
  intent is zero false positives on cross-function passing, accepting the
  gap as a known limitation rather than guessing).
- A tracked-name LOAD that is part of a ``while cond: await asyncio.
  sleep(N)``-style loop's own CONDITION expression is exempt when that
  loop's body contains a yield point — the hand-rolled polling idiom a
  small number of pre-``settle()`` sites use directly. This gate's
  general line-ordering check cannot see this shape as safe on its own
  (the condition's source line sits at or before the sleep inside the
  loop body, even though every iteration AFTER the first re-evaluates
  that condition strictly AFTER the sleep already ran) — handled as its
  own structural exemption instead of trying to make line-ordering
  loop-aware.
- A tracked-name LOAD that occurs textually BEFORE a ``.emit(...)`` call
  known to exist LATER in the same function is exempt regardless of yield
  points — nothing has been emitted yet by the time this read runs, so
  there is nothing it could race (e.g. an initial ``assert not any(...)``
  asserting the collected list starts empty, written before the
  function's own first ``emit()`` call). This exemption requires
  POSITIVE evidence of a later ``.emit(...)`` call visible in this same
  function's own AST — a function with NO visible ``.emit(...)`` call at
  all is NOT exempted this way (the overwhelmingly common real shape is
  the triggering emit happening INSIDE a called production function, e.g.
  ``await handle(op, ctx)``, invisible to this file's own AST; treating
  "no emit call visible here" as "nothing to race" would silently exempt
  nearly every real site).

## Scope

Every ``tests/**/*.py`` file. A LOAD of a tracked name is flagged if no
yield point (as defined above) appears at an earlier line, within the SAME
function, than the read.

This gate's own starting population is zero (all 31 found instances were
fixed in the same PR that added this gate) — any hit here is a new
regression, not inherited debt.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TESTS_DIR = _ROOT / "tests"

# #4966 (lead-coder finding, closing the population by CONSTRUCTION rather
# than by surface pattern — a name-based search for "helper functions that
# look like collect_events" found real, still-broken sites using a
# differently-named local helper on the SAME underlying mechanism, on the
# same night). There are exactly two ways ANYTHING becomes an EventLog
# subscriber: `add_subscriber()` (the sole `self._subscribers.append` call
# site, events.py) and the `EventLog(subscribers=[...])` constructor
# argument. Any call to one of these two methods is therefore a genuine
# subscriber registration, whatever the caller chooses to name the list or
# helper it writes into — the discriminator is "did this call
# add_subscriber()", not "does this function's name look like a collector".
_SUBSCRIBE_METHOD_NAMES = frozenset({"add_subscriber", "subscribe_audit_events"})
_COLLECT_EVENTS_FUNC_NAME = "collect_events"
_SETTLE_FUNC_NAME = "settle"
_DRAIN_METHOD_NAME = "drain"
_SLEEP_METHOD_NAME = "sleep"

# lead-coder finding (measured, not guessed): a fuzzy `"wait" in name`
# substring check is broken two ways — (1) "await" itself CONTAINS "wait"
# as a substring, so it silently accepted every bare-name async helper
# whose name happens to start with "await" (`_await_reply`,
# `_await_scheduled_source_build`, ...), none of which are polling
# helpers; (2) it would also accept `<x>.wait()` (e.g.
# `asyncio.Event.wait()`) if such a call ever matched the Name-callee
# branch, and `Event.wait()` does NOT reliably yield — CPython's own
# implementation returns immediately, without ever yielding, if the
# event is already set. A wrong ACCEPT here is exactly the silent-pass
# class this gate exists to close, so the accepted set is CLOSED
# (mirrors `bounding.py`'s `BOUNDING_KEYS`/`UnknownBoundingKeyError`
# shape) — an unrecognized await is never treated as a yield point.
# `Event.wait` is deliberately NOT in this set.
_POLLING_HELPER_NAMES = frozenset({
    "_wait_for",
    "_wait_for_event",
    "_wait_for_file",
    "_wait_for_calls",
    "_wait_until",
    "wait_until",
    "_poll",
})


def _call_func_name(call: ast.Call) -> "str | None":
    """The plain name of a call's callee, if it's a bare ``Name`` — e.g.
    ``settle(x)`` -> ``"settle"``. ``None`` for anything else (an
    ``Attribute`` call like ``x.drain()`` is handled separately by its own
    caller, not here)."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _tracked_name_from_subscriber_arg(arg: ast.expr) -> "str | None":
    """If *arg* — the argument passed to ``add_subscriber()``/
    ``subscribe_audit_events()`` — is ``<name>.append`` (a bound method
    reference) or a ``lambda`` whose body is a call to ``<name>.append(...)``,
    return ``<name>``: the list this subscription writes into, which
    therefore needs the same settle()-before-read treatment as a
    ``collect_events()`` binding.

    Returns ``None`` for anything else — a hand-rolled ``class XSink:
    def __call__(self, e): self.events.append(e)`` instance, a free
    function, a ``ChatLifecycleForwarder``-style production callable —
    none of these are statically resolvable to a single tracked NAME by
    this rule. This is a disclosed gap, not a claim of total coverage:
    such sites need their own settle() call reviewed by hand; this gate
    finds what the two closed-form shapes above cover, not everything
    that could theoretically race.

    The bare ``<name>.append`` shape's own ``<name>`` Load is exempted
    from pass 3's flagging by the caller (see ``registration_arg_ids`` in
    ``_check_function``) — REGISTERING a subscriber is not itself a read
    that can race anything (nothing has been emitted yet at that point in
    the function); only a LATER read of the tracked list can race."""
    if isinstance(arg, ast.Attribute) and arg.attr == "append" and isinstance(arg.value, ast.Name):
        return arg.value.id
    if isinstance(arg, ast.Lambda):
        body = arg.body
        if (
            isinstance(body, ast.Call)
            and isinstance(body.func, ast.Attribute)
            and body.func.attr == "append"
            and isinstance(body.func.value, ast.Name)
        ):
            return body.func.value.id
    return None


def _is_yield_point(node: ast.Await) -> bool:
    """True if awaiting *node* is safe to read a tracked name after —
    settle()/drain(), ``asyncio.sleep(...)`` specifically (not any
    object's own ``.sleep()`` method), or a name in the CLOSED
    ``_POLLING_HELPER_NAMES`` set. See the module docstring's "What
    counts as a yield point" section and ``_POLLING_HELPER_NAMES``'s own
    comment for why this is a closed enumeration, not a substring match."""
    call = node.value
    if not isinstance(call, ast.Call):
        return False
    name = _call_func_name(call)
    if name == _SETTLE_FUNC_NAME:
        return True
    if name is not None and name in _POLLING_HELPER_NAMES:
        return True
    if isinstance(call.func, ast.Attribute):
        if call.func.attr == _DRAIN_METHOD_NAME:
            return True
        if (
            call.func.attr == _SLEEP_METHOD_NAME
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "asyncio"
        ):
            return True
    return False


def _is_inside_lambda(node: ast.AST, func_body_root: ast.AST) -> bool:
    """True if *node* is nested inside an ``ast.Lambda`` somewhere between
    it and *func_body_root* — a predicate passed to a polling helper, not
    a direct read (see module docstring)."""
    for parent in ast.walk(func_body_root):
        if isinstance(parent, ast.Lambda):
            for child in ast.walk(parent):
                if child is node:
                    return True
    return False


def _is_while_poll_condition(node: ast.AST, func_body_root: ast.AST) -> bool:
    """True if *node* is part of a ``While`` loop's own ``test``
    expression, and that loop's body contains a yield point — the
    ``while cond: await asyncio.sleep(N)`` hand-rolled polling idiom (a
    small number of sites predate ``settle()``/``_wait_for`` and use this
    shape directly).

    A line-number-ordered check (this gate's general approach) cannot see
    this case as safe on its own: the condition's SOURCE line is always
    at or before the sleep inside the loop body, even though on every
    iteration AFTER the first, the condition is re-evaluated strictly
    AFTER that same sleep has already run — the loop's own control flow
    makes it retry-with-yield, which is exactly the property that makes
    ``_wait_for(lambda: ...)``-style polling safe elsewhere in this gate.
    Handled as its own exemption, structurally, rather than trying to
    make the general line-ordering check loop-aware."""
    for parent in ast.walk(func_body_root):
        if not isinstance(parent, ast.While):
            continue
        test_nodes = set(ast.walk(parent.test))
        if node not in test_nodes:
            continue
        body_has_yield = any(
            isinstance(n, ast.Await) and _is_yield_point(n)
            for stmt in parent.body
            for n in ast.walk(stmt)
        )
        if body_has_yield:
            return True
    return False


def _has_loop_context(func: "ast.FunctionDef | ast.AsyncFunctionDef") -> bool:
    """True if *func* is ``async def`` — the ONLY shape where a read
    inside its own body can race the dispatch queue's background
    consumer.

    A plain ``def`` that wraps its work in ``asyncio.run(coro)`` and
    reads a tracked name AFTER that call returns is NOT flagged: #4966's
    ``_dispatch_consumer`` flushes whatever remains queued, synchronously,
    on ``CancelledError`` (the shape ``asyncio.run()``'s own teardown
    delivers when the loop closes) BEFORE that coroutine's task is
    considered done — so by the time ``asyncio.run()`` returns control to
    the plain ``def`` caller, full delivery is already guaranteed by the
    mechanism itself, not by anything the test wrote. Measured directly
    (a synthetic probe against both the pre- and post-#4966 consumer,
    with and without an explicit ``settle()`` call): every event was
    delivered by the time ``asyncio.run()`` returned in every combination
    tried. This is why the earlier ①②③ split narrows to just ① here —
    ② (no loop anywhere) was never in scope (``emit()``'s own no-loop
    inline branch), and ③ (asyncio.run()-wrapped, read after it returns)
    closed at the mechanism level, not the test level.

    A nested ``async def`` INSIDE a plain ``def`` (e.g. an inner
    ``async def _it(): ...`` wrapper passed to ``asyncio.run``) is its
    own separate node ``ast.walk`` visits independently — a read inside
    THAT coroutine, before it returns, is still ① (the mechanism only
    protects against loss at CANCELLATION time, not against a read
    racing the consumer while the coroutine is still normally running) —
    it gets checked on its own by this same function, no special-casing
    needed here."""
    return isinstance(func, ast.AsyncFunctionDef)


def _check_function(func: "ast.FunctionDef | ast.AsyncFunctionDef") -> "list[tuple[int, str]]":
    """Flagged (line, name) pairs for one function body — a tracked name
    (bound from ``collect_events(...)`` or derived one level from an
    already-tracked name) read with no yield point earlier in the SAME
    function. Isolated from file-level scanning so it's directly testable
    per-function."""
    if not _has_loop_context(func):
        return []
    tracked: set[str] = set()
    yield_lines: list[int] = []
    emit_lines: list[int] = []
    registration_arg_ids: set[int] = set()
    flagged: list[tuple[int, str]] = []

    # Pass 1: collect every yield-point line, every `.emit(...)` call line,
    # and every direct collect_events(...) binding, in a single walk
    # (order doesn't matter for this pass — all three are pure
    # membership/line-number facts). `emit_lines` lets pass 3 exempt a
    # read that occurs before ANY emit() in this function — nothing has
    # been emitted yet, so there's nothing for that read to race,
    # regardless of yield points (e.g. an initial `assert not any(...)`
    # asserting the list starts empty, before the function's own first
    # `log.emit(...)` call).
    for node in ast.walk(func):
        if isinstance(node, ast.Await) and _is_yield_point(node):
            yield_lines.append(node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "emit":
                emit_lines.append(node.lineno)
            if node.func.attr in _SUBSCRIBE_METHOD_NAMES and node.args:
                name = _tracked_name_from_subscriber_arg(node.args[0])
                if name is not None:
                    tracked.add(name)
                    # The registration argument itself (`<name>.append`,
                    # a bare bound-method reference — NOT the lambda-
                    # wrapped form) contains a Load of `<name>` that
                    # would otherwise be flagged as an unsafe "read" by
                    # pass 3: nothing has been emitted yet at the point
                    # of REGISTERING a subscriber, so there is nothing
                    # for it to race. Exempt this exact node by identity.
                    arg = node.args[0]
                    if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                        registration_arg_ids.add(id(arg.value))
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            if (
                isinstance(node.value, ast.Call)
                and _call_func_name(node.value) == _COLLECT_EVENTS_FUNC_NAME
            ):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for t in targets:
                    if isinstance(t, ast.Name):
                        tracked.add(t.id)

    # Pass 2: fixpoint-propagate "derived from a tracked name" bindings —
    # a name assigned from an expression that itself contains a Load of an
    # already-tracked name also becomes tracked (covers `blocked = [e for
    # e in collected if ...]`, `x = collected[-1]`, `(a,) = [e for e in
    # collected ...]`, etc. — the real shapes found in the 31-site audit).
    changed = True
    while changed:
        changed = False
        for node in ast.walk(func):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                refs_tracked = any(
                    isinstance(n, ast.Name) and n.id in tracked and isinstance(n.ctx, ast.Load)
                    for n in ast.walk(node.value)
                )
                if not refs_tracked:
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for t in targets:
                    names = (
                        [t] if isinstance(t, ast.Name)
                        else [e for e in ast.walk(t) if isinstance(e, ast.Name)]
                    )
                    for n in names:
                        if n.id not in tracked:
                            tracked.add(n.id)
                            changed = True

    if not tracked:
        return []

    earliest_yield = min(yield_lines) if yield_lines else None

    # Pass 3: flag every LOAD of a tracked name that is not itself the
    # binding assignment, not inside a lambda, and occurs at or before the
    # earliest yield point (or there is no yield point at all).
    for node in ast.walk(func):
        if not (isinstance(node, ast.Name) and node.id in tracked and isinstance(node.ctx, ast.Load)):
            continue
        if _is_inside_lambda(node, func):
            continue
        if _is_while_poll_condition(node, func):
            continue
        if id(node) in registration_arg_ids:
            continue
        if emit_lines and node.lineno < min(emit_lines):
            # Only exempt when we have POSITIVE evidence of a later
            # `.emit(...)` call textually in this same function — an
            # EMPTY `emit_lines` does NOT mean "nothing emitted yet"; the
            # overwhelmingly common shape is the read's own triggering
            # emit happening INSIDE a called production function (e.g.
            # `await handle(op, ctx)`), invisible to this file's own AST.
            # Treating "no emit call visible here" as "nothing to race"
            # would silently exempt nearly every real site — the opposite
            # of this gate's purpose.
            continue
        if earliest_yield is not None and node.lineno > earliest_yield:
            continue
        flagged.append((node.lineno, node.id))

    return flagged


def offending_files(tests_dir: Path = _TESTS_DIR) -> "list[tuple[Path, list[tuple[int, str]]]]":
    """Every ``tests/**/*.py`` file with at least one flagged read, paired
    with its (line, name) hits — the gate's entire decision, isolated from
    CLI/printing so it is directly testable."""
    offenders: list[tuple[Path, list[tuple[int, str]]]] = []
    for path in sorted(tests_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        hits: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                hits.extend(_check_function(node))
        if hits:
            offenders.append((path, sorted(hits)))
    return offenders


def main(
    argv: "list[str] | None" = None,
    *,
    tests_dir: Path = _TESTS_DIR,
    root: Path = _ROOT,
) -> int:
    """#5485: ``tests_dir``/``root`` mirror :func:`offending_files`'s own
    keyword-with-real-default shape — a public seam a test can pass a
    ``tmp_path`` tree through, instead of monkeypatching this module's
    private ``_TESTS_DIR``/``_ROOT`` globals to redirect the real CLI
    entry point at a fixture."""
    del argv  # no options — a whole-tree scan against a baseline of zero
    offenders = offending_files(tests_dir)

    if not offenders:
        print(
            "OK: no collect_events()- or subscriber-derived list is read "
            "without a settle()/drain() (or equivalent polling yield) "
            "earlier in the same function."
        )
        return 0

    print("collect-events-settle gate FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} file(s) read a collect_events()- or "
        "subscriber-derived list "
        "with no settle()/drain()/polling-yield earlier in the same "
        "function (#4965/#4966) — dispatch to that list is asynchronous "
        "whenever a running loop exists, so this read can race the "
        "background consumer and silently miss the event it's checking "
        "for. If the assertion checks ABSENCE, this bug is invisible in "
        "CI (a missed delivery looks identical to a real absence):",
        file=sys.stderr,
    )
    for path, hits in offenders:
        rel = path.relative_to(root)
        for line, name in hits:
            print(f"  {rel}:{line}: read of {name!r}", file=sys.stderr)

    print(
        "\nFix: `await settle(<the EventLog>)` (from "
        "tests._support.events) immediately before the read — or, for an "
        "asyncio.run(coro)-wrapped test, move the read inside the "
        "coroutine (after `await settle(log)`) before it returns, since "
        "the loop closes once asyncio.run() returns.\n"
        "\nThis gate's own starting population is zero, so any hit here "
        "is a new regression, not inherited debt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
