"""Tier 2: OS invariant — #2708 P1 present-sink consumer wiring + forced chainlit fix.

Covers the #2708 P1 seam:
- ``build_scoped_chat_session`` REQUIRES ``presentation_consumer`` (no default) — a
  frontend that omits it fails to construct (compile-time forcing; A1/A2 orphan
  impossible).
- ``OutboxPresentationConsumer.sink(session)`` yields an ``OutboxPresentationRenderer``
  that puts the SAME ``"presentation"`` outbox message as the pre-#2708 uniform default
  (byte-identical).
- chainlit's ``outbox_to_chainlit`` now renders the ``present`` render model from
  ``meta["nodes"]`` (was an empty ``text`` fall-through = the #2688 silent-drop bug).
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from reyn.interfaces.chainlit_app.adapter import outbox_to_chainlit
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.presentation_consumer import OutboxPresentationConsumer
from reyn.runtime.scoped_session_factory import build_scoped_chat_session


class _Session:
    """A minimal real Session stand-in exposing only the ``outbox`` an
    OutboxPresentationRenderer writes to (mirrors test_present_renderer_fp0054_prb)."""

    def __init__(self) -> None:
        self.outbox: asyncio.Queue = asyncio.Queue()


# ── forcing: presentation_consumer is a required no-default kwarg ─────────────


def test_presentation_consumer_is_required_no_default() -> None:
    """Tier 2: build_scoped_chat_session's ``presentation_consumer`` is a REQUIRED
    keyword-only arg (no default) — a frontend cannot silently omit the present sink
    (orphan-impossible by construction, #2708 A1)."""
    sig = inspect.signature(build_scoped_chat_session)
    p = sig.parameters["presentation_consumer"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is inspect.Parameter.empty


def test_build_session_without_consumer_is_a_construction_error() -> None:
    """Tier 2: omitting ``presentation_consumer`` raises at call time (the compile/
    construction-time forcing the required kwarg guarantees). We assert the specific
    missing arg so the failure is the forcing, not an unrelated one."""
    with pytest.raises(TypeError) as ei:
        # Deliberately omit presentation_consumer (and everything else) — the FIRST
        # thing Python reports for a no-default kwarg-only call is the missing arg set.
        build_scoped_chat_session()  # type: ignore[call-arg]
    assert "presentation_consumer" in str(ei.value)


# ── byte-identical: consumer.sink round-trips the outbox present message ──────


def test_outbox_consumer_sink_puts_presentation_message() -> None:
    """Tier 2: OutboxPresentationConsumer.sink(session).render(resolved) puts a
    ``kind="presentation"`` OutboxMessage carrying resolved.nodes onto the session's
    outbox — byte-identical to the pre-#2708 uniform ``OutboxPresentationRenderer(self)``
    default (the sink is now obtained via the consumer instead of hardcoded)."""
    from reyn.core.present.binding import ResolvedPresentation

    session = _Session()
    sink = OutboxPresentationConsumer().sink(session)
    assert sink.surface_name == "inline-cui"  # byte-identical surface

    sink.render(ResolvedPresentation(nodes=[{"component": "text", "text": "hi"}]))
    msg = session.outbox.get_nowait()
    assert msg.kind == "presentation"
    assert msg.meta["nodes"] == [{"component": "text", "text": "hi"}]


# ── forced chainlit fix (#2688): presentation renders instead of dropping ─────


def test_chainlit_renders_presentation_nodes_not_empty() -> None:
    """Tier 2: RED→GREEN for the #2688 chainlit silent-drop. A ``kind="presentation"``
    OutboxMessage (text="", render model in meta["nodes"]) now maps to a chainlit
    payload whose content contains the rendered node text — before #2708 it fell
    through to the generic branch and emitted the empty ``text``."""
    msg = OutboxMessage(
        kind="presentation",
        text="",
        meta={"nodes": [
            {"component": "text", "text": "hello from present"},
            {"component": "code", "language": "python", "text": "x = 1"},
        ]},
    )
    payload = outbox_to_chainlit(msg)
    assert payload is not None
    assert payload.role == "message"
    assert "hello from present" in payload.content
    assert "x = 1" in payload.content
    assert "```python" in payload.content
    # The bug was empty content despite a non-empty render model:
    assert payload.content.strip() != ""


def test_chainlit_present_table_and_list_render() -> None:
    """Tier 2: the chainlit present serializer covers the structured components
    (table / keyvalue / list) so a structured present isn't silently blanked."""
    msg = OutboxMessage(
        kind="presentation",
        text="",
        meta={"nodes": [
            {"component": "list", "items": ["a", "b"]},
            {"component": "keyvalue", "rows": [{"label": "k", "value": "v"}]},
            {"component": "table", "columns": [
                {"header": "H1", "cells": ["r1"]},
                {"header": "H2", "cells": ["r2"]},
            ]},
        ]},
    )
    content = outbox_to_chainlit(msg).content
    assert "- a" in content and "- b" in content
    assert "**k**: v" in content
    assert "| H1 | H2 |" in content
