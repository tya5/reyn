"""Tier 2 (with two Tier 1/structural helpers): #6240 ④ (architect ruling,
issue #6240 comment 5807710323, quoted verbatim in the PR body) —
``RouterHistoryBuffer.spill_turn_content`` no longer appends its own
durable ``spill_record`` from whatever thread calls it (a worker thread,
reached via ``RouterLoopDriver``'s several ``asyncio.to_thread``
dispatches). It now RETURNS the record (extending its existing
``str | None`` return into ``SpillTurnResult(replacement, record)``),
and a caller genuinely on the loop appends it.

Root defect this closes (architect's own #6240 comment): ``Session.
_append_history`` mutates SESSION STATE (``self._next_seq += 1``, the
active segment window, ``self.history``), not just a disk write — it is
NOT synchronized, so reaching it from a worker thread while a
concurrent loop-side ``_append_history`` call is also in flight is a
real, unsynchronized shared-state race. #3 (③, a LATER PR) moves the
disk WRITE off-loop; this PR (④) is what makes sure NOTHING that
mutates session state is ever called off-loop in the first place.

Real ``Session``/``RouterLoopDriver``/``RouterHistoryBuffer``/
``MediaStore`` throughout for the functional (accept) witness — no
mocks (testing policy). The structural (deny/reachability) witnesses
are AST-based, per this PR's own dispatch brief: "AST で
`to_thread` に渡される関数から `_history_appender` へ到達しないことを見
る" — chosen over an in-``_append_history`` "is there a running event
loop" runtime check because the reachability claim is about SOURCE
STRUCTURE (a call this method's body could make, full stop), not about
which thread happens to invoke it on any one particular test run; an
AST check is exhaustive over every code path in the method body, where
a runtime probe would only ever cover the ONE path the test actually
drives.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from pathlib import Path

import pytest

from reyn.runtime.chat_message import ChatMessage, Spillability
from reyn.runtime.services.router_history_buffer import (
    RouterHistoryBuffer,
    SpillTurnResult,
)
from reyn.runtime.services.router_loop_driver import RouterLoopDriver
from reyn.services.compaction.engine import RecoveryLadder
from tests.runtime.test_5296_pr2_byte_reduction_same_turn_retry import (
    _make_spill_session,
    _push,
)


def _func_ast(func) -> ast.FunctionDef:
    """Parse *func*'s own source into its ``FunctionDef`` node — dedented
    first (``inspect.getsource`` on a method keeps the class's own
    indentation, which ``ast.parse`` rejects as invalid syntax)."""
    src = textwrap.dedent(inspect.getsource(func))
    module = ast.parse(src)
    (node,) = module.body
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    return node


def _calls_named_attr(node: ast.AST, attr_name: str) -> bool:
    """True if *node*'s own subtree contains a CALL (not merely a
    reference) whose callee is ``self.<attr_name>(...)`` or
    ``<anything>.<attr_name>(...)`` — used both ways below: to prove an
    ABSENCE (spill_turn_content) and a PRESENCE (the loop-side
    callers)."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Attribute) and func.attr == attr_name:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────
# Witness 1 — structural deny: spill_turn_content itself never appends.
# This IS #6240 ④'s own definition (see architect's comment, quoted in
# this file's own module docstring): "worker thread は record を返す。
# append は loop が行う."
# ─────────────────────────────────────────────────────────────────────────


def test_spill_turn_content_body_never_calls_the_history_appender():
    """Tier 1: structural/AST — ``spill_turn_content``'s own function
    body contains NO call to ``self._history_appender(...)`` — the exact
    call #6240 ④ removed. This is the method every worker-thread spill
    dispatch (``_spill_batch_within_face``, itself reached via
    ``asyncio.to_thread`` both directly and via ``shrink_pool_after_
    overflow``) ultimately calls, so an absence HERE is an absence on
    every off-loop path by construction — never merely "the one path this
    test happened to drive."

    Strip-falsifier (performed during review, RED text captured
    verbatim): re-adding ``self._history_appender(record)`` right after
    the record's construction in ``spill_turn_content`` (restoring the
    pre-#6240 body) makes this test fail with::

        AssertionError: spill_turn_content's own body must never call
        self._history_appender(...) directly -- #6240 (4) moved that
        call to a loop-side caller
    """
    node = _func_ast(RouterHistoryBuffer.spill_turn_content)
    assert not _calls_named_attr(node, "_history_appender"), (
        "spill_turn_content's own body must never call "
        "self._history_appender(...) directly -- #6240 (4) moved that "
        "call to a loop-side caller"
    )


def test_spill_batch_within_face_body_never_calls_the_history_appender():
    """Tier 1: structural/AST — the SAME property one layer up:
    ``_spill_batch_within_face`` (the function directly dispatched via
    ``asyncio.to_thread``, both by ``_attempt_reactive_spill`` and
    transitively via ``_spill_batch_for_retry``) also never appends
    directly; it only ever writes into its own ``record_sink`` parameter
    (mutation, not an append call named ``_history_appender`` or
    ``_append_history_fn``).

    Strip-falsifier (performed during review, RED text captured
    verbatim): replacing the ``record_sink.append(_outcome.record)``
    line with ``self._append_history_fn(_outcome.record)`` (calling the
    loop-side appender directly from this worker-thread-dispatched
    method) makes this test fail with::

        AssertionError: _spill_batch_within_face's own body must never
        call the loop-side appender directly -- #6240 (4)'s whole point
        is that this method runs OFF the loop
    """
    node = _func_ast(RouterLoopDriver._spill_batch_within_face)
    assert not _calls_named_attr(node, "_history_appender"), (
        "_spill_batch_within_face's own body must never call the "
        "loop-side appender directly -- #6240 (4)'s whole point is "
        "that this method runs OFF the loop"
    )
    assert not _calls_named_attr(node, "_append_history_fn"), (
        "_spill_batch_within_face's own body must never call the "
        "loop-side appender directly -- #6240 (4)'s whole point is "
        "that this method runs OFF the loop"
    )


# ─────────────────────────────────────────────────────────────────────────
# Witness 1, present side — the record DOES get appended, just from a
# caller genuinely on the loop. Structural pairing for the deny above:
# without this, "never appends" could vacuously describe a record that
# is never appended AT ALL, anywhere.
# ─────────────────────────────────────────────────────────────────────────


def test_attempt_reactive_spill_body_does_call_the_history_appender():
    """Tier 1: structural/AST — the PRESENT half of the deny/present
    pair above: ``_attempt_reactive_spill`` (an ``async def`` — no
    ``asyncio.to_thread`` wraps ITS OWN frame; only the ``_spill_batch_
    within_face`` call inside it does) DOES call
    ``self._append_history_fn(...)`` — the loop-side append #6240 ④
    moved the responsibility TO, not merely REMOVED.

    Strip-falsifier (performed during review, RED text captured
    verbatim): removing the ``for _record in _records: self.
    _append_history_fn(_record)`` loop (leaving spill's own durable
    record permanently stranded in ``_records`` and never persisted)
    makes this test fail with::

        AssertionError: _attempt_reactive_spill must call
        self._append_history_fn(...) -- otherwise #6240 (4)'s deny
        witness above would be vacuous (nothing ever appends the record)
    """
    node = _func_ast(RouterLoopDriver._attempt_reactive_spill)
    assert _calls_named_attr(node, "_append_history_fn"), (
        "_attempt_reactive_spill must call self._append_history_fn(...) "
        "-- otherwise #6240 (4)'s deny witness above would be vacuous "
        "(nothing ever appends the record)"
    )


def test_recovery_ladder_drain_body_does_call_the_history_appender():
    """Tier 1: structural/AST — the retry-ladder's own equivalent:
    ``RecoveryLadder._drain_spill_records`` (called immediately after
    EVERY ``await asyncio.to_thread(shrink_pool_after_overflow, ...)``
    inside ``_run_one_iteration`` -- itself an ``async def`` frame, on
    the loop) DOES call ``self._append_history_fn(...)``.

    Strip-falsifier (performed during review, RED text captured
    verbatim): replacing the method's body with a bare ``return`` (no
    drain at all) makes this test fail with::

        AssertionError: RecoveryLadder._drain_spill_records must call
        self._append_history_fn(...) -- this is the loop-side drain
        point #6240 (4) added inside the retry ladder itself, needed so
        a LATER shrink attempt within the same episode sees an EARLIER
        one's own spill via is_already_spilled
    """
    node = _func_ast(RecoveryLadder._drain_spill_records)
    assert _calls_named_attr(node, "_append_history_fn"), (
        "RecoveryLadder._drain_spill_records must call "
        "self._append_history_fn(...) -- this is the loop-side drain "
        "point #6240 (4) added inside the retry ladder itself, needed "
        "so a LATER shrink attempt within the same episode sees an "
        "EARLIER one's own spill via is_already_spilled"
    )


# ─────────────────────────────────────────────────────────────────────────
# Witness 2 — spill's own CALCULATION stays off-loop: to_thread was never
# removed. This is the discriminator against lead-coder's rejected (b)
# ("pull spill/_spill_batch_* back onto the loop, remove to_thread") --
# without this witness, a change satisfying witness 1 by ALSO deleting
# asyncio.to_thread would pass witness 1 and be the WRONG fix.
# ─────────────────────────────────────────────────────────────────────────


def _awaits_to_thread_with(func, target_name: str) -> bool:
    """True if *func*'s own body contains
    ``await asyncio.to_thread(<something ending in target_name>, ...)``."""
    node = _func_ast(func)
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Await):
            continue
        call = sub.value
        if not isinstance(call, ast.Call):
            continue
        callee = call.func
        if not (
            isinstance(callee, ast.Attribute)
            and callee.attr == "to_thread"
            and isinstance(callee.value, ast.Name)
            and callee.value.id == "asyncio"
        ):
            continue
        if not call.args:
            continue
        first = call.args[0]
        if isinstance(first, ast.Attribute) and first.attr == target_name:
            return True
        if isinstance(first, ast.Name) and first.id == target_name:
            return True
    return False


def test_attempt_reactive_spill_still_dispatches_the_batch_via_to_thread():
    """Tier 1: structural/AST — #6240 ④'s own explicit non-goal:
    ``to_thread`` is NOT removed. ``_attempt_reactive_spill`` must still
    dispatch ``_spill_batch_within_face`` (the actual cap-estimate/hash/
    write work) via ``await asyncio.to_thread(...)`` -- spill's own
    CALCULATION stays off the loop; only the durable APPEND moved.

    Strip-falsifier (performed during review, RED text captured
    verbatim): replacing ``await asyncio.to_thread(self._spill_batch_
    within_face, ...)`` with a direct (on-loop) call ``self._spill_
    batch_within_face(...)`` makes this test fail with::

        AssertionError: _attempt_reactive_spill must still dispatch
        _spill_batch_within_face via asyncio.to_thread -- #6240 (4)
        moves the APPEND onto the loop, never the spill CALCULATION
        itself (that was lead-coder's rejected (b), not what shipped)
    """
    target = RouterLoopDriver._attempt_reactive_spill
    assert _awaits_to_thread_with(target, "_spill_batch_within_face"), (
        "_attempt_reactive_spill must still dispatch "
        "_spill_batch_within_face via asyncio.to_thread -- #6240 (4) "
        "moves the APPEND onto the loop, never the spill CALCULATION "
        "itself (that was lead-coder's rejected (b), not what shipped)"
    )


def test_recovery_ladder_still_dispatches_shrink_via_to_thread():
    """Tier 1: structural/AST — the retry-ladder's own equivalent non-
    goal check: ``RecoveryLadder._run_one_iteration`` must still
    dispatch ``shrink_pool_after_overflow`` (which is what actually
    invokes ``spill_fn`` -> ``_spill_batch_for_retry`` ->
    ``_spill_batch_within_face``) via ``await asyncio.to_thread(...)``.

    Strip-falsifier (performed during review, RED text captured
    verbatim): replacing ``self._compact_attempt_len = await asyncio.
    to_thread(shrink_pool_after_overflow, ...)`` with a direct (on-loop)
    call ``shrink_pool_after_overflow(...)`` makes this test fail
    with::

        AssertionError: RecoveryLadder._run_one_iteration must still
        dispatch shrink_pool_after_overflow via asyncio.to_thread --
        #6240 (4) never pulls the shrink/spill calculation itself onto
        the loop
    """
    target = RecoveryLadder._run_one_iteration
    assert _awaits_to_thread_with(target, "shrink_pool_after_overflow"), (
        "RecoveryLadder._run_one_iteration must still dispatch "
        "shrink_pool_after_overflow via asyncio.to_thread -- #6240 (4) "
        "never pulls the shrink/spill calculation itself onto the loop"
    )


# ─────────────────────────────────────────────────────────────────────────
# Witness 3 — accept: a spilled record still reaches durable history the
# SAME way it always did, end to end, through the real production
# call path -- the test itself never appends anything by hand (unlike
# the pre-existing #5612/#5720/... tests, which drive spill_turn_content
# DIRECTLY and therefore must play the loop-side appender role
# themselves -- see those files' own #6240 (4) comments). This test
# drives the REAL loop-side caller instead, so the append witnessed here
# is production's own, not the test harness standing in for it.
# ─────────────────────────────────────────────────────────────────────────


def test_reactive_spill_record_reaches_durable_history_without_the_test_appending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #6240 ④ accept — driving ``_attempt_reactive_spill``
    directly (the real production loop-side caller, exactly as
    ``test_5615_attempt_reactive_spill_reachability.py`` already does)
    against a genuinely oversized candidate durably appends exactly ONE
    ``spill_record`` to ``session.history`` -- with NO call in this test
    to ``session._append_history`` or any ``.record`` unpacking. If
    #6240 (4)'s own loop-side append were ever silently dropped (e.g. an
    empty ``record_sink`` that spill_turn_content's caller forgot to
    wire), this durable record would simply never appear -- this test
    would catch that by finding zero, not by erroring.

    Strip-falsifier (performed during review, RED text captured
    verbatim): commenting out the ``for _record in _records: self.
    _append_history_fn(_record)`` loop in ``_attempt_reactive_spill``
    makes this test fail with::

        ValueError: not enough values to unpack (expected 1, got 0)

    (raised by this test's own ``(only_record,) = records`` unpack —
    the durable record simply never arrived).
    """
    session = _make_spill_session(tmp_path, monkeypatch, t_max=None)
    huge = "R" * 50_000
    _push(session, "user", "look something up", spillability=Spillability.NEVER)
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")

    progressed = asyncio.run(
        session._loop_driver._attempt_reactive_spill(chain_id="c1"),
    )
    assert progressed is True, (
        "sanity: the oversized candidate must have genuinely been spilled"
    )

    records = [m for m in session.history if m.role == "spill_record"]
    # Unpack-must-flip (this repo's own established idiom for "exactly
    # one" — e.g. test_5564_history_content_spill_origin_agnostic.py's
    # own `(tool_path, user_path) = written`): raises ValueError instead
    # of a bare count comparison if this is ever 0 or >1.
    (only_record,) = records
    assert isinstance(only_record, ChatMessage)


def test_spill_turn_result_is_a_namedtuple_extension_not_a_new_shape():
    """Tier 1: #6240 ④'s own stated constraint ("同関数は既に `-> str |
    None` を返しています ∴ 返り値の拡張だけです") -- ``SpillTurnResult`` is
    a 2-field ``NamedTuple`` whose FIRST field is positionally the exact
    same value ``spill_turn_content`` always returned (the replacement
    text or ``None``), so any existing call site that only ever cared
    about that ONE value can still get it positionally
    (``spill_turn_content(...)[0]``) -- an extension, not a replacement
    of the prior contract."""
    fields = SpillTurnResult._fields
    assert fields == ("replacement", "record"), (
        f"expected the replacement text to stay field 0 (positional "
        f"compatibility with the pre-#6240 str-only return) and record "
        f"to be the NEW, second field -- got {fields!r}"
    )
