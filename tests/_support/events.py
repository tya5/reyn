"""``collect_events`` — the test-side mirror of production's real subscriber
mechanism, for #3868 PR-2/PR-3.

architect's design (#3868 comment 5229065773): PR-3 deletes ``EventLog._events``
/ ``all()`` / ``to_json()`` entirely — the derived ``ingested`` state (PR-1)
means the OS itself never needs a full-history read again. But ~230 test call
sites still read ``.all()``/``.to_json()`` to assert on what happened during a
test run. A "retain history only during tests" flag was explicitly REJECTED:
it would make tests exercise a code path production never runs, so a green
test would no longer witness production behavior (#3037's dead-permission-gate
shape, generalized).

The fix instead: production ALREADY has the real mechanism for "collect
everything that happens" — ``EventStore`` is wired as an ``EventLog``
subscriber (``session.py``), so every emitted ``Event`` reaches it via the
SAME ``add_subscriber``/``emit`` path every other subscriber uses. Tests use
that exact mechanism instead of a special read-back API: a plain list,
appended to by a subscriber function, is functionally identical to what
``EventStore`` does, minus the disk write.

Call :func:`collect_events` ONCE, right after the ``EventLog`` (or
``EventLog``-holding object) a test will emit into is constructed — BEFORE
any ``emit()`` call the test wants to observe. The returned list is LIVE: it
keeps growing as the log emits, so a test may reference it repeatedly (a
polling loop like ``lambda: any(e.type == "x" for e in collected)`` works
unchanged) — it is not a one-shot snapshot the way ``.all()`` was. Emits that
happened BEFORE :func:`collect_events` was called are NOT retroactively
captured (a subscriber only sees what is emitted after it is added) — this is
the one behavior difference from ``.all()``, and it is why the call must move
to right after construction, not stay at the assertion site.

#4961 C / #4966 (architect ruling): dispatch to ``collected`` moved off
``emit()``'s synchronous caller onto a background consumer task — a
POLLING read (the ``lambda: any(...)`` shape above, or anything driven
through ``_wait_for``-style retry) still works unchanged, because the
act of polling yields between attempts and gives the consumer a chance
to run. What breaks is a read that happens SYNCHRONOUSLY right after an
``await`` that triggered the emit, with no yield in between — the
consumer may not have run yet, so ``collected`` can still be missing
the event. If a test reads ``collected`` this way, make the "I assumed
delivery already happened" assumption explicit in the code by awaiting
:func:`settle` on the same log immediately before the read.
"""
from __future__ import annotations

from typing import Any


def collect_events(log: Any) -> list[Any]:
    """Return a list that live-collects every :class:`Event` *log* emits from
    this call onward, via a real ``add_subscriber`` — the same mechanism
    production's ``EventStore`` uses, not a read-back of history.

    Call this immediately after constructing *log* (or as soon as a test
    holds a reference to it), before any ``emit()`` the test cares about."""
    collected: list[Any] = []
    log.add_subscriber(collected.append)
    return collected


async def settle(log: Any) -> None:
    """#4961 C / #4966 (architect ruling): wait for *log*'s dispatch queue to
    finish delivering everything emitted so far to its subscribers —
    including a :func:`collect_events` list — before a synchronous read.

    A thin, explicitly-named wrapper over ``EventLog.drain()``. Exists so a
    test that reads a collected list right after an ``emit()``-triggering
    ``await`` (no polling loop in between) can make that "delivery already
    happened" assumption visible in the test's own code, at the exact spot
    it depends on: ``await settle(log)`` immediately before the read. A
    polling read (``lambda: any(e.type == "x" for e in collected)``, or
    anything driven through a ``_wait_for``-style retry) does not need this
    — polling yields between attempts and gives the consumer a chance to
    run regardless."""
    await log.drain()
