"""Tier 2: #6230 stage 3 — a sentinel-family ``OutboxMessage.kind`` requires
a genuine, non-empty ``text`` AT CONSTRUCTION, closing the recurrence class
stages 1/2 fixed one instance of each (issue thread ruling, architect +
lead-coder). Same structural shape #5047 already gave the intervention
family (``OutboxMessage.__post_init__``, ``_INTERVENTION_FAMILY_KINDS``):
"must carry its own identity at construction time, never recovered later by
position or absence."

**Population** (issue thread, architect's own correction of their first
"4" count — a literal grep's own range, not the true population): every
``__``-prefixed member of :data:`~reyn.runtime.outbox.VOCABULARY` EXCEPT
``__end__``. Today that is exactly 3 — ``__open_artifact__``,
``__copy_last_reply__``, ``__rewind_list__`` — the three slash-command
sentinels (`/open`, `/copy`, `/rewind`). ``__end__`` is excluded: it is
transport control (the stream terminator), not a response to anything a
user typed, and its ``text`` is ``""`` BY CONSTRUCTION at its one call site
(``transport/agui/endpoint.py:1002``).

This test file hardcodes those 3 literal kind strings rather than importing
the module-private ``_SENTINEL_FAMILY_KINDS`` set — asserting on the
PUBLIC construction contract (``OutboxMessage(kind=..., text=...)`` raises
or not), never on private module state.

This file also pins that the raise this invariant produces is FAIL-VISIBLE
where it can actually fire, not silently swallowed: the one real producer
that could ever hit it (a slash-command handler) runs inside
``execute_slash_command``'s own ``try/except Exception`` (``dispatch.py``)
— that boundary logs (``logger.exception``) AND displays
``/{name} failed: {detail}`` to the user (``dispatch.py``'s own docstring:
"A handler must never kill the loop"); it does not discard the failure.
The construction call itself (inside the handler body) is NOT wrapped in
its own try, so nothing between the raise and that one boundary can
silently eat it.

Real instances throughout — no ``MagicMock``/``AsyncMock``/``patch``.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash import REGISTRY, SlashCommand
from reyn.interfaces.slash.dispatch import execute_slash_command
from reyn.runtime.outbox import OutboxMessage
from tests._support.slash import slash_ctx

_SENTINEL_KINDS = ("__open_artifact__", "__copy_last_reply__", "__rewind_list__")


@pytest.mark.parametrize("kind", _SENTINEL_KINDS)
def test_a_sentinel_kind_with_empty_text_cannot_be_constructed(kind: str) -> None:
    """Tier 2: the degenerate case IS the invariant — a sentinel-shaped
    frame with ``text=""`` must fail at construction, not render a blank
    line downstream. Strip target: delete the stage 3 check in
    ``__post_init__`` → this raises nothing → RED (construction succeeds
    with an empty ``text``)."""
    with pytest.raises(ValueError, match="non-empty"):
        OutboxMessage(kind=kind, text="")


@pytest.mark.parametrize("kind", _SENTINEL_KINDS)
def test_a_sentinel_kind_with_real_text_constructs_normally(kind: str) -> None:
    """Tier 2: the invariant only rejects the EMPTY case — a genuine
    producer (stage 1's own `/open` · `/copy`, unchanged `/rewind`) keeps
    constructing exactly as before."""
    msg = OutboxMessage(kind=kind, text="a human reads this")
    assert msg.kind == kind
    assert msg.text == "a human reads this"


def test_end_is_excluded_by_its_own_nature_not_a_literal_carve_out_alone() -> None:
    """Tier 2: ``__end__`` is the ONE dunder-kind the stage 3 invariant does
    NOT apply to — it is transport control (the stream terminator), never a
    slash-command response, and its ``text`` is ``""`` BY CONSTRUCTION at
    its one real call site. Constructing it with an empty ``text`` must
    keep succeeding — the same call ``transport/agui/endpoint.py:1002``
    already makes today. Strip target: widen the stage 3 population to
    include ``__end__`` → this raises → RED (a legible line would have to
    be FABRICATED for a frame that, by design, carries none)."""
    msg = OutboxMessage(kind="__end__", text="")
    assert msg.kind == "__end__"
    assert msg.text == ""


def test_an_unvocabularied_sentinel_shaped_kind_still_fails_the_vocabulary_gate_first() -> None:
    """Tier 2: a brand-new, not-yet-declared ``__``-prefixed kind is
    rejected by the PRE-EXISTING vocabulary gate (ADR-0039 P6b), not by
    the stage 3 text check — the two are independent mechanisms, and the
    vocabulary gate runs first. This is also why the stage 3 population
    can never silently miss a FUTURE sentinel: nothing not already in
    ``VOCABULARY`` can reach ``__init__`` at all, so a kind that later
    joins ``DISPLAY_KINDS``/``CONTROL_KINDS`` is auto-enrolled in the
    stage 3 population the same commit that adds it, with no second edit
    required."""
    with pytest.raises(ValueError, match="closed vocabulary"):
        OutboxMessage(kind="__not_a_declared_sentinel__", text="")


def test_from_wire_stays_lenient_on_a_sentinel_kind_with_no_text() -> None:
    """Tier 2: ``OutboxMessage.from_wire`` deliberately bypasses
    ``__post_init__`` entirely (its own docstring: an untrusted wire frame
    "MUST degrade gracefully ... never fail-close") — the stage 3
    invariant must not leak onto that path. A wire-decoded sentinel with a
    genuinely missing ``text`` key still constructs (degrading through
    stage 2's ``legible_degrade_text`` at RENDER time instead of raising
    at construction). Strip target: route ``from_wire`` through
    ``__post_init__`` → this raises → RED (the untrusted-wire leniency
    contract broken, the same failure mode ``test_outbox_vocabulary.py``'s
    ``test_from_wire_is_lenient_on_an_unknown_kind`` already pins for the
    kind check itself)."""
    msg = OutboxMessage.from_wire(kind="__copy_last_reply__", meta={"arg": "2"})
    assert msg.kind == "__copy_last_reply__"
    assert msg.text == ""


async def _xtest_degenerate_sentinel_cmd(ctx, args: str) -> None:  # noqa: ANN001
    ctx.transport.put_display(OutboxMessage(kind="__copy_last_reply__", text=""))


REGISTRY.register(SlashCommand(
    name="xtest_6230_stage3_degenerate_sentinel",
    summary="test-only: constructs an empty-text sentinel to strip-falsify #6230 stage 3",
    handler=_xtest_degenerate_sentinel_cmd,
    locus="client",
    hidden=True,
))


@pytest.mark.asyncio
async def test_a_construction_raise_at_the_one_real_producer_site_is_fail_visible_not_swallowed() -> None:
    """Tier 2: the ONE real place this invariant could ever fire in
    production is a slash-command handler constructing its own sentinel
    (``copy.py``/``open_artifact.py``/``rewind.py``) — this test proves
    that specific raise reaches a user-visible surface through the REAL
    ``execute_slash_command`` boundary (``dispatch.py``), rather than
    vanishing into some earlier catch. Registers a throwaway handler
    (``xtest_...``, same convention ``test_slash_see_also_field.py`` uses)
    that reproduces the exact degenerate construction, run through the
    real dispatch executor and a real ``SlashContext``/transport — no
    fake of ``execute_slash_command`` itself.

    Strip target: this test does not strip ``__post_init__`` again (that
    is covered above); it strip-falsifies the CLAIM that the raise is
    fail-visible by asserting what ``dispatch.py``'s own
    ``except Exception`` branch actually does — verified by reading that
    branch (``logger.exception`` + a displayed ``error`` line), not
    merely asserted."""
    ctx = slash_ctx()
    ran = await execute_slash_command(ctx, "xtest_6230_stage3_degenerate_sentinel", "")
    # execute_slash_command's own contract: a raising handler is CONTAINED
    # (returns True, "ran" — never propagates to kill the caller's loop).
    assert ran is True
    error_text = ctx.transport.error_text()
    assert "xtest_6230_stage3_degenerate_sentinel failed" in error_text
    assert "ValueError" in error_text
