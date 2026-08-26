"""Tier 2: a subscriber can declare which event kinds it wants.

Every subscriber used to be called for every event and filter itself on the way
in — the same decision written out at each of them (AG-UI's set membership,
A2A's type check, the OTel exporter's elif chain), and a call paid per event per
subscriber for the ones that only ever wanted a few kinds.

Declaring the interest at registration lets the dispatcher skip. Not declaring
keeps the old contract exactly, because a subscriber whose interest is computed
per event cannot honestly declare a fixed one.
"""
from __future__ import annotations

import asyncio

from reyn.core.events.events import EventLog


def _log() -> EventLog:
    return EventLog(agent_id="test", emitter="test")


def test_a_declared_subscriber_is_not_called_for_other_kinds() -> None:
    """Tier 2: the dispatcher skips, rather than the subscriber returning."""
    log = _log()
    seen: list[str] = []
    log.add_subscriber(lambda e: seen.append(e.type), kinds={"wanted"})

    log.emit("wanted")
    log.emit("unwanted")

    assert seen == ["wanted"]


def test_an_undeclared_subscriber_still_gets_everything() -> None:
    """Tier 2: falsification pair — without this, a dispatcher that skipped
    everything unless declared would still pass the test above, and every
    existing subscriber would silently stop receiving.
    """
    log = _log()
    seen: list[str] = []
    log.add_subscriber(lambda e: seen.append(e.type))

    log.emit("wanted")
    log.emit("unwanted")

    assert seen == ["wanted", "unwanted"]


def test_declaring_several_kinds_admits_each_of_them() -> None:
    """Tier 2: the declaration is a set, not a single kind."""
    log = _log()
    seen: list[str] = []
    log.add_subscriber(lambda e: seen.append(e.type), kinds={"a", "b"})

    log.emit("a")
    log.emit("c")
    log.emit("b")

    assert seen == ["a", "b"]


def test_two_subscribers_declare_independently() -> None:
    """Tier 2: one subscriber's declaration does not narrow another's — the
    filter is per subscriber, not a property of the log.
    """
    log = _log()
    narrow: list[str] = []
    wide: list[str] = []
    log.add_subscriber(lambda e: narrow.append(e.type), kinds={"a"})
    log.add_subscriber(lambda e: wide.append(e.type))

    log.emit("a")
    log.emit("b")

    assert narrow == ["a"]
    assert wide == ["a", "b"]


def test_the_declaration_holds_on_the_async_dispatch_path() -> None:
    """Tier 2: ``emit()`` dispatches inline with no running loop and through a
    consumer task with one. Both paths carry the same filter — a declaration
    honoured on only one of them would depend on whether a loop happened to be
    running, which is not something a caller chooses.
    """
    seen: list[str] = []

    async def _run() -> None:
        log = _log()
        log.add_subscriber(lambda e: seen.append(e.type), kinds={"wanted"})
        log.emit("wanted")
        log.emit("unwanted")
        await log.drain()

    asyncio.run(_run())

    assert seen == ["wanted"]


def test_the_subscribers_view_still_lists_a_declared_subscriber() -> None:
    """Tier 2: declaring an interest does not change what ``subscribers``
    reports — existing readers compare it against the callables they passed.
    """
    log = _log()

    def sub(event) -> None:
        return None

    log.add_subscriber(sub, kinds={"a"})

    assert log.subscribers == [sub]


def test_removing_a_declared_subscriber_forgets_its_declaration() -> None:
    """Tier 2: ``remove_subscriber`` exists so a scoped consumer subscribing
    per call does not grow the subscriber list without bound. A declaration
    that outlived its subscriber would grow at exactly that rate — and it
    holds the callable, so whatever a closure captured goes with it.

    Witnessed through the public surface only: re-adding the SAME callable
    with no declaration must receive every kind. If the stale entry survived,
    the old declaration would still be filtering it.
    """
    log = _log()
    seen: list[str] = []

    def sub(event) -> None:
        seen.append(event.type)

    log.add_subscriber(sub, kinds={"a"})
    assert log.remove_subscriber(sub) is True
    log.add_subscriber(sub)

    log.emit("a")
    log.emit("b")

    assert seen == ["a", "b"]

