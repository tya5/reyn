"""Tier 2: #5870 stage 1 — ``stall_trace.arm(..., repeat=False)`` actually
behaves as the dead-man's switch ``TextualChatApp._watch_loop_
responsiveness`` re-arms every tick: a re-arm that lands before the
previous one's own deadline cancels and replaces it (no dump, even across
several such re-arms whose TOTAL elapsed time exceeds a single deadline),
and a deadline nothing re-arms in time for actually fires, dumping the
main thread's stack — mid-stall, on ``faulthandler``'s own OS thread,
independent of whatever blocked the interpreter.

Genuinely duration-as-subject, not a smuggled sleep-and-poll: the whole
point under test is whether a REAL, deliberate synchronous block gets its
stack dumped by a background OS-thread timer while the interpreter itself
cannot run any Python code at all — there is no way to fake that
without an actual block, and no way to observe it without one either.
Both the threshold and the block duration are explicit LOCAL constants
this file chose (never a hidden default read from elsewhere), and neither
test waits/polls for a condition to become true over time — one real
synchronous block, one file read, no ceiling of its own (CI's own
``--timeout=120`` is the kill switch, per testing policy).

``tests/runtime/test_4405_stall_trace_wiring.py``'s own module docstring
explains why IT avoids exactly this shape for the turn-arm bracket — that
file's subject is WIRING (does the right value reach arm/disarm), which a
real threshold-crossing would only add noise to. This file's subject is
different: whether ``repeat=False`` itself, as a mechanism, produces the
dump-on-miss / silence-on-time behavior stage 1 is built on. Isolated
from the real ``TextualChatApp`` tick loop deliberately — driving the
full async worker through a Textual ``run_test()`` pilot would still
bottom out at this same primitive, at the cost of far more moving parts
(worker scheduling, real wall-clock ``asyncio.sleep`` waits) for no
extra coverage; the wiring half (does the tripwire's own worker actually
call this with the right arguments) is pinned separately, without any
real delay, in ``tests/interfaces/test_3671_stall_trace_startup_wiring.py``.
"""
from __future__ import annotations

import time
from pathlib import Path

from reyn.runtime import stall_trace

#: The dead-man's-switch deadline this file arms with — well under the
#: real block below, and well under stall_trace.py's own faulthandler
#: dump latency (its docstring's own reasoning: an OS-thread timer, not
#: bound by whatever the interpreter itself is doing).
_DEADLINE_S = 0.1

#: A deliberate, real synchronous block — long enough to clear
#: _DEADLINE_S with margin (a flaky-scheduling false negative would read
#: as "no dump", the wrong direction to be flaky in for an accept test).
_BLOCK_S = 0.4

#: Four simulated on-time ticks, each well inside _DEADLINE_S — the
#: re-arm-every-tick shape #5870 stage 1 actually uses. Their SUM
#: (4 * 0.03 = 0.12s) exceeds _DEADLINE_S itself, which is the point:
#: only a single UNBROKEN gap past the deadline should ever dump, never
#: an accumulation of smaller ones.
_ON_TIME_TICK_S = 0.03


def test_repeat_false_dumps_a_stack_when_nothing_re_arms_it_in_time(
    tmp_path: Path,
) -> None:
    """Tier 2: accept — a real block past the deadline leaves a stack dump
    in the file ``arm`` was told to use."""
    dump_path = tmp_path / "dump.log"
    with open(dump_path, "w", encoding="utf-8") as handle:
        stall_trace.arm(_DEADLINE_S, repeat=False, file=handle)
        try:
            time.sleep(_BLOCK_S)  # the real, deliberate block — the subject itself
        finally:
            stall_trace.disarm()

    dumped = dump_path.read_text(encoding="utf-8")
    assert "Thread" in dumped, (
        f"faulthandler did not dump a stack past a {_DEADLINE_S}s deadline "
        f"held for {_BLOCK_S}s: {dumped!r}"
    )


def test_repeat_false_never_fires_across_several_on_time_re_arms(
    tmp_path: Path,
) -> None:
    """Tier 2: accept-side pair, and the falsifier for the test above —
    strip the per-tick re-arm (arm once for the whole span instead) and
    this file's own first test already shows what a missed deadline
    looks like. Here, each re-arm lands well before the previous one's
    deadline (mirrors #5870's real per-tick shape), so the pending
    one-shot is replaced every time and never gets the chance to fire —
    even though the accumulated real time across all four re-arms
    (4 * _ON_TIME_TICK_S) exceeds the single _DEADLINE_S used above.
    """
    dump_path = tmp_path / "dump.log"
    with open(dump_path, "w", encoding="utf-8") as handle:
        try:
            for _ in range(4):
                stall_trace.arm(_DEADLINE_S, repeat=False, file=handle)
                time.sleep(_ON_TIME_TICK_S)
        finally:
            stall_trace.disarm()

    dumped = dump_path.read_text(encoding="utf-8")
    assert dumped == "", (
        f"a re-arm that always lands before its own deadline must never "
        f"dump: {dumped!r}"
    )
