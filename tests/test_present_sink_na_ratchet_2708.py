"""Tier 2: OS invariant — #2708 P1 NA-present-surface reviewed ratchet.

A surface with NO human presentation drain may use a ``NullPresentationSink``
(documented no-op) — but ONLY through ``NullPresentationConsumer``, and ONLY for a
surface named in the reviewed ``_NA_PRESENTATION_SURFACES`` frozenset. This ratchet
(the FP-0056 admin-6 equality model) prevents a NEW human/visible surface from
silently NA-dodging (passing a Null sink instead of implementing a real present
consumer): adding a member forces this equality assertion to be updated in the same
review, and constructing a Null consumer for a non-member raises.
"""
from __future__ import annotations

import pytest

from reyn.runtime.presentation_consumer import (
    _NA_PRESENTATION_SURFACES,
    NullPresentationConsumer,
    NullPresentationSink,
)


def test_na_surface_set_is_the_reviewed_frozenset() -> None:
    """Tier 2: the NA-present surfaces are EXACTLY the reviewed set (web / mcp /
    dogfood). A new member (esp. a human surface) must be added here deliberately —
    the equality ratchet blocks a silent NA-dodge."""
    assert _NA_PRESENTATION_SURFACES == frozenset({"web", "mcp", "dogfood"})


def test_null_consumer_refuses_non_reviewed_surface() -> None:
    """Tier 2: a Null present sink is refused for a surface NOT in the reviewed NA
    set (e.g. a human surface trying to dodge providing a real consumer)."""
    with pytest.raises(ValueError):
        NullPresentationConsumer("chat")
    with pytest.raises(ValueError):
        NullPresentationConsumer("chainlit")


def test_null_consumer_yields_noop_sink_for_reviewed_surface() -> None:
    """Tier 2: each reviewed NA surface constructs a NullPresentationConsumer whose
    sink is a NullPresentationSink (render is a no-op that never raises)."""

    class _StubSession:
        pass

    for surface in ("web", "mcp", "dogfood"):
        consumer = NullPresentationConsumer(surface)
        assert consumer.surface == surface
        sink = consumer.sink(_StubSession())
        assert isinstance(sink, NullPresentationSink)
        # no-op render must not raise (fire-and-continue display contract)
        sink.render(object())
