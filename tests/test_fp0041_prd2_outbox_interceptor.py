"""Tier 2: FP-0041 #489 PR-D2 — Slack outbound via outbox interceptor.

End-to-end reply path completion for the Slack chat-transport. PR-D
landed inbound (= Slack → Reyn inbox). PR-D2 completes outbound:

  inbox payload reply_to (= ExternalRef from PR-D handler)
    → Session captures into self._last_reply_to
    → agent reply via _put_outbox
    → reply_to defaults from _last_reply_to (when not explicit)
    → _outbox_interceptor invoked (= PR-D2 wiring)
    → make_outbox_interceptor dispatches via route_to_mcp (PR-C)
    → Slack MCP server chat.postMessage (= operator-installed)

Tests:

  1. Sender attribution also captures reply_to into _last_reply_to
  2. _put_outbox defaults missing reply_to from _last_reply_to
  3. _put_outbox invokes interceptor when reply_to is ExternalRef
  4. Interceptor return True → message NOT queued (= consumed by
     external transport, no TUI duplicate)
  5. Interceptor return False → message falls through to queue
  6. Interceptor exception → falls through to queue (= defensive)
  7. Non-ExternalRef reply_to (= TuiRef etc.) → interceptor skipped
  8. make_outbox_interceptor dispatch matrix (= ok/error/unconfigured/
     non-dispatchable kind / non-ExternalRef)
  9. ReynConfig.external_transports loads from yaml + lazy import OK

Tier 2 because this is the load-bearing wiring that makes Slack
chat-transport actually work end-to-end. Without it, PR-D inbound
delivers messages but no reply ever reaches Slack.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.external_routing import (
    ExternalTransportEntry,
    ExternalTransportRouting,
    make_outbox_interceptor,
)
from reyn.chat.outbox import OutboxMessage
from reyn.chat.session import Session
from reyn.chat.transport import ExternalRef, TuiRef
from reyn.core.events.state_log import StateLog


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> Session:
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / f"{agent_name}.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


# ── Section 1: reply_to capture from inbox attribution ────────────────


def test_attribution_captures_reply_to_from_payload(tmp_path):
    """Tier 2: when inbox payload carries ``reply_to``, the dispatch
    attribution helper saves it into ``self._last_reply_to`` for
    later outbox emission.
    """
    session = _make_session(tmp_path)
    rt = ExternalRef(transport="slack", destination={"channel": "C1"})
    session._handle_sender_attribution({
        "sender": "slack:U1",
        "reply_to": rt,
    })
    assert session.last_reply_to is rt


def test_attribution_does_not_clear_reply_to_when_payload_lacks_it(tmp_path):
    """Tier 2: a follow-up payload without reply_to preserves the
    previously captured value (= same-thread follow-ups inherit the
    original reply_to without each producer needing to re-attach).
    """
    session = _make_session(tmp_path)
    rt = ExternalRef(transport="slack", destination={"channel": "C1"})
    session._handle_sender_attribution({"sender": "slack:U1", "reply_to": rt})
    # Second payload, no reply_to.
    session._handle_sender_attribution({"sender": "slack:U1"})
    assert session.last_reply_to is rt


def test_attribution_updates_reply_to_when_payload_carries_new_one(tmp_path):
    """Tier 2: a new reply_to in the payload replaces the prior value
    (= different Slack thread / different transport).
    """
    session = _make_session(tmp_path)
    session._last_reply_to = ExternalRef(transport="slack", destination={"channel": "C1"})
    new_rt = ExternalRef(transport="slack", destination={"channel": "C2"})
    session._handle_sender_attribution({
        "sender": "slack:U1", "reply_to": new_rt,
    })
    assert session.last_reply_to is new_rt


# ── Section 2: _put_outbox reply_to defaulting + interceptor ──────────


@pytest.mark.asyncio
async def test_put_outbox_inherits_reply_to_from_last(tmp_path):
    """Tier 2: an OutboxMessage without explicit reply_to picks up
    the session's ``_last_reply_to`` so the agent's reply
    automatically routes back.
    """
    session = _make_session(tmp_path)
    rt = ExternalRef(transport="slack", destination={"channel": "C1"})
    session._last_reply_to = rt

    msg = OutboxMessage(kind="agent", text="hello back")
    await session._put_outbox(msg)
    queued = await session.outbox.get()
    assert queued.reply_to is rt


@pytest.mark.asyncio
async def test_put_outbox_keeps_explicit_reply_to(tmp_path):
    """Tier 2: an explicit reply_to on the OutboxMessage is NOT
    overwritten by ``_last_reply_to`` defaulting (= producer code
    that explicitly sets reply_to wins).
    """
    session = _make_session(tmp_path)
    session._last_reply_to = ExternalRef(transport="slack", destination={"channel": "C1"})

    explicit = TuiRef()
    msg = OutboxMessage(kind="agent", text="x", reply_to=explicit)
    await session._put_outbox(msg)
    queued = await session.outbox.get()
    assert queued.reply_to is explicit


@pytest.mark.asyncio
async def test_put_outbox_invokes_interceptor_for_external_ref(tmp_path):
    """Tier 2: interceptor is called when reply_to is ExternalRef and
    one is registered. Return True → message NOT queued.
    """
    session = _make_session(tmp_path)
    intercepted: list = []

    async def _interceptor(msg):
        intercepted.append(msg)
        return True

    session._outbox_interceptor = _interceptor
    rt = ExternalRef(transport="slack", destination={"channel": "C1"})
    msg = OutboxMessage(kind="agent", text="hi", reply_to=rt)
    await session._put_outbox(msg)

    assert intercepted, "expected interceptor to be called at least once"
    assert intercepted[0].reply_to is rt
    # Not queued.
    assert session.outbox.empty()


@pytest.mark.asyncio
async def test_put_outbox_interceptor_returns_false_falls_through(tmp_path):
    """Tier 2: when the interceptor returns False (= "I can't handle
    this, send it to TUI"), the message proceeds to the normal
    outbox queue. Used for unconfigured transports so the operator
    sees the message somewhere.
    """
    session = _make_session(tmp_path)

    async def _passthrough(msg):
        return False

    session._outbox_interceptor = _passthrough
    rt = ExternalRef(transport="unknown", destination={})
    msg = OutboxMessage(kind="agent", text="x", reply_to=rt)
    await session._put_outbox(msg)

    queued = await session.outbox.get()
    assert queued.reply_to is rt


@pytest.mark.asyncio
async def test_put_outbox_interceptor_exception_falls_through(tmp_path):
    """Tier 2: a buggy interceptor (= raises) does NOT crash
    ``_put_outbox``; the message proceeds to the queue so the user
    sees something. Defensive isolation.
    """
    session = _make_session(tmp_path)

    async def _boom(msg):
        raise RuntimeError("interceptor bug")

    session._outbox_interceptor = _boom
    rt = ExternalRef(transport="slack", destination={"channel": "C1"})
    msg = OutboxMessage(kind="agent", text="x", reply_to=rt)
    await session._put_outbox(msg)

    queued = await session.outbox.get()
    assert queued.reply_to is rt


@pytest.mark.asyncio
async def test_put_outbox_interceptor_skipped_for_non_external_ref(tmp_path):
    """Tier 2: a TuiRef (= local terminal reply) does NOT trigger the
    interceptor. Only ExternalRef paths.
    """
    session = _make_session(tmp_path)
    called: list = []

    async def _interceptor(msg):
        called.append(msg)
        return True

    session._outbox_interceptor = _interceptor
    msg = OutboxMessage(kind="agent", text="x", reply_to=TuiRef())
    await session._put_outbox(msg)

    assert called == []
    queued = await session.outbox.get()
    assert queued is msg


# ── Section 3: make_outbox_interceptor dispatch matrix ────────────────


@pytest.mark.asyncio
async def test_make_interceptor_dispatches_external_ref():
    """Tier 2: the factory-produced interceptor routes ExternalRef
    messages through ``route_to_mcp`` and the supplied
    ``mcp_dispatcher``.
    """
    dispatched: list = []

    async def _mcp(tool, args):
        dispatched.append((tool, args))
        return None

    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="slack__chat_postMessage",
            args_template={"channel": "{destination.channel}", "text": "{text}"},
        ),
    })
    interceptor = make_outbox_interceptor(
        routing=routing, mcp_dispatcher=_mcp,
    )
    msg = OutboxMessage(
        kind="agent", text="hello",
        reply_to=ExternalRef(transport="slack", destination={"channel": "C1"}),
    )
    handled = await interceptor(msg)
    assert handled is True
    assert dispatched == [(
        "slack__chat_postMessage",
        {"channel": "C1", "text": "hello"},
    )]


@pytest.mark.asyncio
async def test_make_interceptor_returns_false_for_unconfigured():
    """Tier 2: when the transport isn't configured (= operator hasn't
    declared an entry), the interceptor returns False so the message
    falls through to TUI display — operator sees the missing config.
    """
    async def _mcp(tool, args):
        pytest.fail("dispatcher should not be called for unconfigured")

    routing = ExternalTransportRouting()  # empty
    interceptor = make_outbox_interceptor(
        routing=routing, mcp_dispatcher=_mcp,
    )
    msg = OutboxMessage(
        kind="agent", text="x",
        reply_to=ExternalRef(transport="slack", destination={}),
    )
    assert (await interceptor(msg)) is False


@pytest.mark.asyncio
async def test_make_interceptor_returns_true_on_dispatcher_error():
    """Tier 2: a dispatcher exception → ``route_to_mcp`` returns
    ``status="error"`` → interceptor consumes the message (= True)
    so a flaky Slack server doesn't flood TUI with retries.
    """
    async def _failing(tool, args):
        raise RuntimeError("Slack unreachable")

    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="slack__chat_postMessage",
            args_template={"text": "{text}"},
        ),
    })
    interceptor = make_outbox_interceptor(
        routing=routing, mcp_dispatcher=_failing,
    )
    msg = OutboxMessage(
        kind="agent", text="x",
        reply_to=ExternalRef(transport="slack", destination={}),
    )
    assert (await interceptor(msg)) is True


@pytest.mark.asyncio
async def test_make_interceptor_skips_non_dispatchable_kinds():
    """Tier 2: status / trace / __end__ / intervention messages are
    NOT relayed to external transport even if they happen to carry
    ExternalRef. Only ``agent`` (= LLM reply) by default.
    """
    async def _mcp(tool, args):
        pytest.fail("dispatcher should not be called for non-agent kind")

    routing = ExternalTransportRouting(transports={
        "slack": ExternalTransportEntry(
            mcp_tool="slack__post", args_template={"text": "{text}"},
        ),
    })
    interceptor = make_outbox_interceptor(
        routing=routing, mcp_dispatcher=_mcp,
    )
    rt = ExternalRef(transport="slack", destination={})
    for kind in ("status", "trace", "__end__", "intervention", "error"):
        msg = OutboxMessage(kind=kind, text="x", reply_to=rt)
        assert (await interceptor(msg)) is False, f"kind={kind} unexpectedly handled"


@pytest.mark.asyncio
async def test_make_interceptor_skips_non_external_ref():
    """Tier 2: a TuiRef reply_to is not the interceptor's concern;
    return False to let the normal flow take over.
    """
    async def _mcp(tool, args):
        pytest.fail("dispatcher should not be called for TuiRef")

    routing = ExternalTransportRouting()
    interceptor = make_outbox_interceptor(
        routing=routing, mcp_dispatcher=_mcp,
    )
    msg = OutboxMessage(kind="agent", text="x", reply_to=TuiRef())
    assert (await interceptor(msg)) is False


# ── Section 4: ReynConfig.external_transports wiring ──────────────────


def test_reyn_config_external_transports_defaults_to_empty(tmp_path, monkeypatch):
    """Tier 2: a project without ``external_transports:`` loads cleanly
    with an empty routing config (= no Slack/LINE configured).
    """
    from reyn.config import load_config

    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    cfg = load_config(cwd=tmp_path)

    assert isinstance(cfg.external_transports, ExternalTransportRouting)
    assert cfg.external_transports.transports == {}


def test_reyn_config_external_transports_parses_well_formed_yaml(
    tmp_path, monkeypatch,
):
    """Tier 2b: a well-formed ``external_transports:`` section (FP-0041 #489 PR-D2 config wire)
    parses into ``ReynConfig.external_transports`` with the documented dataclass
    shape. Operator can declare Slack + LINE routing in reyn.yaml.
    """
    from reyn.config import load_config

    (tmp_path / "reyn.yaml").write_text(
        "model: standard\n"
        "external_transports:\n"
        "  slack:\n"
        "    mcp_tool: slack__chat_postMessage\n"
        "    args_template:\n"
        "      channel: \"{destination.channel}\"\n"
        "      text: \"{text}\"\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = load_config(cwd=tmp_path)

    entry = cfg.external_transports.get("slack")
    assert entry is not None
    assert entry.mcp_tool == "slack__chat_postMessage"
    assert entry.args_template["channel"] == "{destination.channel}"
