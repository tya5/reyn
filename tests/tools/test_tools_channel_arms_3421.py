"""Tier 2: #3421 — "there are no tools" and "this transport has no ``tools=``
channel" are distinguishable at the TYPE level.

Before this issue, ``Presentation`` said both with the same value: ``[]``. The
second meaning was stated in prose at three call sites and in the type at none,
so no consumer could act on it — ``capability_visibility`` carried a comment
reading "llm_tools_payload is genuinely [] (no tools= schema)" precisely because
the value could not say so itself.

The acceptance condition the issue set is the one asserted here: the two are
distinguishable, **not merely commented apart**. So the arms are compared as
values (an empty ``AdvertisedTools`` is not a ``NoToolsChannel``) rather than by
checking that each render happens to look right, and the wire view is asserted to
collapse them — because that collapse is the reason a single list could pass for
both, and it stays correct at the provider boundary while ceasing to be the only
thing a consumer can see.

The registry arm enumerates the live ``(scheme, transport)`` table rather than
naming cells, so a cell added later cannot escape the agreement.
"""
from __future__ import annotations

import pytest

from reyn.tools.encoders import encoder_for_transport
from reyn.tools.exposure import Exposure, FunctionDescriptor
from reyn.tools.scheme import (
    AdvertisedTools,
    NoToolsChannel,
    Presentation,
    advertised_entries,
)
from reyn.tools.transport import (
    Transport,
    resolve_scheme_for_transport,
    valid_scheme_transport_pairs,
)


def _descriptor(name: str) -> FunctionDescriptor:
    return FunctionDescriptor(
        name=name, description="", parameters={"type": "object", "properties": {}},
    )


def test_an_empty_channel_is_not_an_absent_one() -> None:
    """Tier 2: the two answers are different VALUES, though they render the same
    payload on the wire.

    The left-hand side is what ``tool_calls`` produces when every descriptor has
    been narrowed away — a channel that exists and is empty. The right-hand side
    is what ``content_fence`` produces always — no channel at all. The equality
    assertion is the whole issue in one line: before #3421 both were ``[]`` and
    this comparison could not be written."""
    narrowed_to_nothing = encoder_for_transport(Transport.TOOL_CALLS).encode_tools(
        Exposure(descriptors=()),
    )
    no_channel = encoder_for_transport(Transport.CONTENT_FENCE).encode_tools(
        Exposure(descriptors=()),
    )

    assert narrowed_to_nothing != no_channel
    assert isinstance(narrowed_to_nothing, AdvertisedTools)
    assert isinstance(no_channel, NoToolsChannel)
    # …and the reason a single list could stand in for both: the WIRE view is
    # identical. That collapse is still correct where the payload is handed to a
    # provider; what changed is that it is no longer the only view available.
    assert advertised_entries(narrowed_to_nothing) == advertised_entries(no_channel) == []


def test_a_populated_channel_carries_its_entries_through_the_wire_view() -> None:
    """Tier 2: ``advertised_entries`` is the entries themselves on the populated
    arm — the union does not cost the ordinary path anything.

    Guards the mirror-image failure of the one above: an accessor that returned
    ``[]`` for every arm would satisfy that test and silently un-advertise every
    tool in production."""
    channel = encoder_for_transport(Transport.TOOL_CALLS).encode_tools(
        Exposure(descriptors=(_descriptor("read_file"), _descriptor("web_fetch"))),
    )
    names = [e["function"]["name"] for e in advertised_entries(channel)]
    assert names == ["read_file", "web_fetch"]


def test_a_presentation_with_no_channel_must_name_its_dispatchable_set() -> None:
    """Tier 2: #1618 root-1 as a construction-time gate.

    "An empty advertisement must not become an empty dispatch gate" held by
    convention in every ``content_fence`` cell and was checkable nowhere: with
    ``[]`` meaning both things, ``dispatchable_catalog=None`` looked like an
    ordinary tool_calls presentation. On the ``NoToolsChannel`` arm it is a cell
    that dispatches nothing at all, which no cell wants and which would answer
    every in-code call with ``unknown_tool``.

    ``[]`` still passes — a cell that genuinely dispatches nothing says so. The
    refusal is for the cell that never answered the question."""
    with pytest.raises(ValueError, match="dispatchable_catalog"):
        Presentation(tools_channel=NoToolsChannel())

    explicitly_empty = Presentation(
        tools_channel=NoToolsChannel(), dispatchable_catalog=[],
    )
    assert explicitly_empty.dispatchable_catalog == []

    # The gate is arm-specific: an advertised channel keys the gate on its own
    # payload, so ``None`` is the correct answer there and must stay legal.
    assert Presentation(
        tools_channel=AdvertisedTools(entries=[]),
    ).dispatchable_catalog is None


@pytest.mark.parametrize(
    ("scheme_name", "transport"),
    sorted(valid_scheme_transport_pairs(), key=lambda p: (p[0], p[1].value)),
)
def test_every_registered_cell_declares_the_arm_its_transport_implies(
    scheme_name: str, transport: Transport,
) -> None:
    """Tier 2: transport ⇒ arm, over the LIVE registry.

    Enumerated from ``valid_scheme_transport_pairs()`` rather than hand-listed,
    so a cell registered later is covered without anyone remembering to add it —
    the same reason the retired #3376 oracle enumerated instead of listing.

    Asserted at the ENCODER, which is where the answer is decided: a cell whose
    ``build_presentation`` chose the arm itself would be re-deciding a transport
    property per cell, which is what the one-encoder-per-transport rule exists to
    prevent."""
    resolve_scheme_for_transport(scheme_name, transport)  # the cell resolves
    encoder = encoder_for_transport(transport)
    channel = encoder.encode_tools(Exposure(descriptors=(_descriptor("x"),)))

    expected = AdvertisedTools if transport is Transport.TOOL_CALLS else NoToolsChannel
    assert isinstance(channel, expected), (
        f"the {scheme_name}|{transport.value} cell's encoder produced "
        f"{type(channel).__name__}; a transport's answer to 'do I have a tools= "
        "channel' is a property of the transport, not of the cell"
    )
