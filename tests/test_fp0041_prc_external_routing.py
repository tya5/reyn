"""Tier 2: FP-0041 #489 PR-C — external transport routing via MCP.

Pure routing primitive shipped without outbox wiring (= wiring lands
in PR-D Slack handler). Tests cover:

  1. ``ExternalRef`` variant added to TransportRef union with
     transport + destination fields.
  2. ``parse_external_transports`` accepts well-formed YAML config;
     rejects malformed entries silently.
  3. ``build_mcp_args`` template substitution: ``{text}`` and
     ``{destination.<key>}`` placeholders, nested dicts / lists,
     missing keys = empty string.
  4. ``route_to_mcp`` flow: unconfigured transport / dispatcher
     exception / successful dispatch each return the expected
     ``RouteResult`` status.
  5. ``route_to_mcp`` passes correctly-substituted args to the
     dispatcher (= integration of the substitution + lookup).

Tier 2 because the routing primitive is the foundation for any
Slack/LINE/Discord chat-transport reply path; a regression in
substitution semantics or status reporting silently degrades the
PR-D end-to-end Slack handler.
"""
from __future__ import annotations

import pytest

from reyn.chat.external_routing import (
    ExternalTransportEntry,
    ExternalTransportRouting,
    RouteResult,
    build_mcp_args,
    parse_external_transports,
    route_to_mcp,
)
from reyn.chat.transport import ExternalRef

# ── ExternalRef variant ────────────────────────────────────────────────


def test_external_ref_carries_transport_and_destination():
    """Tier 2: ``ExternalRef`` is a frozen dataclass with the two
    documented fields. Pinning the shape so PR-D / future transports
    can rely on a stable envelope contract.
    """
    ref = ExternalRef(
        transport="slack",
        destination={"channel": "C123", "thread_ts": "1234.5678"},
    )
    assert ref.transport == "slack"
    assert ref.destination["channel"] == "C123"


def test_external_ref_is_transport_ref_union_member():
    """Tier 2: ``ExternalRef`` is part of the ``TransportRef`` union
    so existing reply_to typing accepts it.
    """
    from reyn.chat.transport import TransportRef

    # The union check is structural at runtime; we verify that an
    # ExternalRef instance is recognised as one of the union variants
    # by checking the __args__ tuple.
    variants = getattr(TransportRef, "__args__", ())
    assert ExternalRef in variants


# ── parse_external_transports ─────────────────────────────────────────


def test_parse_returns_empty_routing_for_none():
    """Tier 2b: None input produces an empty ExternalTransportRouting (safe default)."""
    assert parse_external_transports(None) == ExternalTransportRouting()


def test_parse_returns_empty_routing_for_non_dict():
    """Tier 2b: non-dict inputs produce an empty ExternalTransportRouting (safe default)."""
    assert parse_external_transports([1, 2, 3]) == ExternalTransportRouting()
    assert parse_external_transports("not a dict") == ExternalTransportRouting()


def test_parse_accepts_well_formed_entry():
    """Tier 2: a complete entry produces the documented dataclass shape."""
    raw = {
        "slack": {
            "mcp_tool": "slack__chat_postMessage",
            "args_template": {
                "channel": "{destination.channel}",
                "text": "{text}",
            },
        },
    }
    routing = parse_external_transports(raw)
    entry = routing.get("slack")
    assert entry is not None
    assert entry.mcp_tool == "slack__chat_postMessage"
    assert entry.args_template["channel"] == "{destination.channel}"
    assert entry.args_template["text"] == "{text}"


def test_parse_skips_entries_with_missing_mcp_tool():
    """Tier 2: an entry without ``mcp_tool`` is silently skipped so
    one bad operator config doesn't take down all transports.
    """
    raw = {
        "good": {
            "mcp_tool": "good__send",
            "args_template": {"x": "y"},
        },
        "bad_no_tool": {
            "args_template": {"x": "y"},
        },
        "bad_empty_tool": {
            "mcp_tool": "",
            "args_template": {"x": "y"},
        },
    }
    routing = parse_external_transports(raw)
    assert routing.get("good") is not None
    assert routing.get("bad_no_tool") is None
    assert routing.get("bad_empty_tool") is None


def test_parse_normalises_missing_args_template_to_empty_dict():
    """Tier 2: an entry without ``args_template`` is accepted with an
    empty template (= caller may dispatch with no substitution).
    """
    raw = {
        "ping": {
            "mcp_tool": "monitor__ping",
        },
    }
    routing = parse_external_transports(raw)
    entry = routing.get("ping")
    assert entry is not None
    assert entry.args_template == {}


# ── build_mcp_args (= placeholder substitution) ────────────────────────


def test_build_mcp_args_substitutes_text_placeholder():
    """Tier 2: ``{text}`` is replaced with the reply text."""
    template = {"body": "Hello: {text}"}
    out = build_mcp_args(template, text="world", destination={})
    assert out == {"body": "Hello: world"}


def test_build_mcp_args_substitutes_destination_key_placeholder():
    """Tier 2: ``{destination.<key>}`` is replaced with the dict value."""
    template = {"channel": "{destination.channel}"}
    out = build_mcp_args(
        template, text="t", destination={"channel": "C123"},
    )
    assert out == {"channel": "C123"}


def test_build_mcp_args_missing_destination_key_becomes_empty_string():
    """Tier 2: a placeholder referencing a key not in destination
    resolves to an empty string (= no KeyError, defensive).
    """
    template = {"thread_ts": "{destination.thread_ts}"}
    out = build_mcp_args(template, text="t", destination={"channel": "C123"})
    assert out == {"thread_ts": ""}


def test_build_mcp_args_substitutes_in_nested_dict_and_list():
    """Tier 2: substitution walks into nested dicts and lists so
    structured templates (= LINE's ``messages: [{type, text}]``)
    work end-to-end.
    """
    template = {
        "messages": [
            {"type": "text", "text": "{text}"},
        ],
        "metadata": {"to": "{destination.user_id}"},
    }
    out = build_mcp_args(
        template, text="hello",
        destination={"user_id": "U999"},
    )
    assert out == {
        "messages": [{"type": "text", "text": "hello"}],
        "metadata": {"to": "U999"},
    }


def test_build_mcp_args_unknown_placeholder_left_literal():
    """Tier 2: an unknown placeholder (= operator typo) passes through
    as a literal string instead of crashing. Mistakes show up in the
    MCP tool's args at dispatch time rather than at template build.
    """
    template = {"x": "{unknown_placeholder}"}
    out = build_mcp_args(template, text="t", destination={})
    assert out == {"x": "{unknown_placeholder}"}


def test_build_mcp_args_non_str_values_passthrough():
    """Tier 2: integers / booleans / None pass through unchanged."""
    template = {"limit": 10, "verify": True, "extra": None}
    out = build_mcp_args(template, text="t", destination={})
    assert out == {"limit": 10, "verify": True, "extra": None}


# ── route_to_mcp dispatch flow ────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_returns_unconfigured_when_transport_missing():
    """Tier 2: an unknown transport returns ``status="unconfigured"``
    without calling the dispatcher. Lets the caller log a clear
    operator-actionable error.
    """
    calls = []

    async def _dispatcher(tool, args):
        calls.append((tool, args))

    routing = ExternalTransportRouting()  # empty
    result = await route_to_mcp(
        "slack", {}, "hi",
        routing=routing, mcp_dispatcher=_dispatcher,
    )
    assert result.status == "unconfigured"
    assert result.transport == "slack"
    assert result.mcp_tool == ""
    assert calls == []


@pytest.mark.asyncio
async def test_route_dispatches_and_substitutes_for_configured_transport():
    """Tier 2: a configured transport substitutes the template and
    passes the resolved args to the dispatcher; returns ``status="ok"``.
    """
    dispatched = []

    async def _dispatcher(tool, args):
        dispatched.append((tool, args))

    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="slack__chat_postMessage",
            args_template={
                "channel": "{destination.channel}",
                "thread_ts": "{destination.thread_ts}",
                "text": "{text}",
            },
        ),
    })
    result = await route_to_mcp(
        "slack",
        {"channel": "C123", "thread_ts": "1.5"},
        "Hello",
        routing=routing,
        mcp_dispatcher=_dispatcher,
    )
    assert result.status == "ok"
    assert result.mcp_tool == "slack__chat_postMessage"
    assert dispatched == [(
        "slack__chat_postMessage",
        {"channel": "C123", "thread_ts": "1.5", "text": "Hello"},
    )]


@pytest.mark.asyncio
async def test_route_returns_error_on_dispatcher_exception():
    """Tier 2: dispatcher exception is caught + recorded in
    ``RouteResult`` instead of propagating. Lets the outbox
    subscriber log and continue with the next message instead of
    crashing the routing layer.
    """
    async def _failing(tool, args):
        raise RuntimeError("MCP server unreachable")

    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="slack__chat_postMessage",
            args_template={"text": "{text}"},
        ),
    })
    result = await route_to_mcp(
        "slack", {}, "hi", routing=routing, mcp_dispatcher=_failing,
    )
    assert result.status == "error"
    assert result.mcp_tool == "slack__chat_postMessage"
    assert "MCP server unreachable" in result.detail


@pytest.mark.asyncio
async def test_route_result_carries_detail_text_for_each_status():
    """Tier 2: ``RouteResult.detail`` is non-empty for each status so
    operators get a clear log line regardless of outcome.
    """
    # ok
    async def _ok(tool, args):
        return None

    routing = ExternalTransportRouting(transports={
        "x": ExternalTransportEntry(mcp_tool="x__send", args_template={}),
    })
    r = await route_to_mcp("x", {}, "t", routing=routing, mcp_dispatcher=_ok)
    assert r.status == "ok"
    assert r.detail  # non-empty

    # unconfigured
    r = await route_to_mcp(
        "missing", {}, "t",
        routing=ExternalTransportRouting(), mcp_dispatcher=_ok,
    )
    assert r.status == "unconfigured"
    assert r.detail

    # error
    async def _err(tool, args):
        raise ValueError("nope")

    r = await route_to_mcp("x", {}, "t", routing=routing, mcp_dispatcher=_err)
    assert r.status == "error"
    assert r.detail
