"""Tier 2: #5891 (c) — the real join, through BOTH production halves.

Architect's own correction of the original #5891 design ("a reader can
JOIN `tool_returned` to its history row on data both sides already
have"): that premise was false — `call_id` identifies a whole litellm
ROUND (shared by every `tool_calls` entry in it), while a history row's
own `tool_call_id` (`ChatMessage.tool_call_id` = `tc["id"]`) is per-call,
so the SAME tool called twice in one round produces two history rows a
`call_id`-only join cannot tell apart. `DispatchContext.tool_call_id`
(`tests/core/test_5891_tool_call_id_audit_field.py` pins this half in
isolation) is meant to close that gap — THIS file proves it actually
does, end to end: two real `dispatch_tool` calls (the SAME tool, one
round, two different `tc["id"]`s) each emit a `tool_returned` event via
the REAL `Session`'s own `EventLog` (`collect_events`, the production
subscriber mechanism, #3868/#5467 — never a read-back of history, never
a hand-rolled sink), and the SAME two `tc` dicts drive a REAL
`RouterLoop.feedback()` + `persist_feedback()` call, the exact production
tool-row write path #5896 stage ① built. Every `tool_returned.
tool_call_id` names exactly one history row, and that row already has a
real `content_ref` — the join `#5891 (c)` promises, demonstrated rather
than argued.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import MultimodalConfig
from reyn.config.chat import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.core.dispatch import DispatchContext, dispatch_tool
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import CONTENT_REF_META_KEY
from reyn.runtime.router_loop import RouterLoop
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle

_MODEL = "gpt-4o"
_BODY_1 = "\n".join(f"line {i}: " + "x" * 60 for i in range(3000))
_BODY_2 = "\n".join(f"line {i}: " + "z" * 60 for i in range(3000))


def _session(agent_name: str, tmp_path: Path):
    return make_session(
        agent_name=agent_name,
        multimodal_config=MultimodalConfig(),
        state_log=StateLog(tmp_path / f"{agent_name}.wal"),
        snapshot_path=tmp_path / f"{agent_name}.snap.json",
        safety=SafetyConfig(loop=LoopConfig(), on_limit=OnLimitConfig(mode="unattended")),
    )


def _mcp_content(body: str) -> dict:
    """Same shape a real MCP tool handler returns -- the `dispatch_tool`
    invoker's own return value, wrapped by `dispatch_tool` itself into
    `{"status": "ok", "data": <this>}` (matching
    `test_5896_history_content_ref.py`'s own `_mcp_env` shape, minus the
    outer wrap this test lets `dispatch_tool` perform for real instead of
    hand-building it)."""
    return {"kind": "mcp", "status": "ok", "server": "s", "tool": "t",
            "content": body, "media_blocks": []}


@pytest.mark.asyncio
async def test_two_tool_returned_events_join_1to1_to_two_history_rows_with_content_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: LOAD-BEARING — #5891 (c)'s own acceptance, items ① and ②.
    Same tool ("mcp"), one round (`call_id="round_1"`), two calls
    (`tc["id"]` = "call_1"/"call_2") -- the exact shape `call_id` alone
    cannot disambiguate. Drives real `dispatch_tool` for the audit half
    and real `RouterLoop.feedback`/`persist_feedback` for the history
    half, using the SAME two `tc` dicts for both, then joins purely on
    `tool_call_id` -- never on `call_id`, order, or content sniffing.

    strip: dropping `tool_call_id=ctx.tool_call_id` from dispatcher.py's
    `tool_returned` emit turns the join assertion red (no `tool_call_id`
    key to join on at all)."""
    monkeypatch.chdir(tmp_path)
    session = _session("join-agent", tmp_path)
    collected = collect_events(session)

    tc1 = {"id": "call_1", "type": "function", "function": {"name": "mcp", "arguments": "{}"}}
    tc2 = {"id": "call_2", "type": "function", "function": {"name": "mcp", "arguments": "{}"}}

    async def _invoker_1(args: dict) -> dict:
        return _mcp_content(_BODY_1)

    async def _invoker_2(args: dict) -> dict:
        return _mcp_content(_BODY_2)

    def _ctx(tool_call_id: str) -> DispatchContext:
        return DispatchContext(
            caller_kind="router",
            caller_id=session.agent_name,
            chain_id="c1",
            tool_catalog={"mcp": {"function": {"name": "mcp"}}},
            events=session.router_host.events,
            contextual=None,
            call_id="round_1",
            tool_call_id=tool_call_id,
        )

    result1 = await dispatch_tool(name="mcp", args={}, ctx=_ctx("call_1"), invoker=_invoker_1)
    result2 = await dispatch_tool(name="mcp", args={}, ctx=_ctx("call_2"), invoker=_invoker_2)
    # #5865/_dispatch_resolved's own tail tags the invoked identity so
    # feedback()'s canonicalizer knows what was called -- done manually
    # here since this test calls dispatch_tool directly, bypassing
    # _dispatch_resolved's own plumbing.
    result1.setdefault("_canonical_source", "mcp")
    result2.setdefault("_canonical_source", "mcp")

    await settle(session)
    _first_event, _second_event = (  # exactly 2, or unpacking itself raises
        e for e in collected if e.type == "tool_returned"
    )
    tool_returned = [_first_event, _second_event]
    audit_ids = {e.data["tool_call_id"] for e in tool_returned}
    assert audit_ids == {"call_1", "call_2"}, (
        f"both events must carry their OWN, distinct tool_call_id, got {audit_ids!r}"
    )
    assert {e.data["call_id"] for e in tool_returned} == {"round_1"}, (
        "both really do share one round's call_id -- the precondition "
        "that makes tool_call_id necessary in the first place"
    )

    from reyn.tools.scheme import ExecutionResult

    loop = RouterLoop(host=session.router_host, chain_id="c1", router_model=_MODEL)
    loop.feedback(ExecutionResult(
        tool_calls=[tc1, tc2], tool_results=[result1, result2], assistant_content="",
    ))
    await loop.persist_feedback()

    _first_row, _second_row = (  # exactly 2, or unpacking itself raises
        m for m in session.history if m.role == "tool"
    )
    tool_rows = [_first_row, _second_row]
    history_ids = {row.tool_call_id for row in tool_rows}
    assert history_ids == audit_ids, (
        "the audit events and the history rows must name the SAME set of "
        "tool_call_ids -- the join key must not drift between the two halves"
    )

    # The join itself: each audit event's tool_call_id resolves to exactly
    # one history row, and that row already carries a real content_ref
    # (#5896 stage ①) -- the #5891 (c) promise, not merely both sides
    # independently having SOME id.
    rows_by_id = {row.tool_call_id: row for row in tool_rows}
    bodies_by_id = {"call_1": _BODY_1, "call_2": _BODY_2}
    for event in tool_returned:
        tool_call_id = event.data["tool_call_id"]
        row = rows_by_id[tool_call_id]
        ref = row.meta.get(CONTENT_REF_META_KEY)
        assert ref, f"history row for {tool_call_id!r} has no content_ref, meta={row.meta!r}"
        body_file = tmp_path / ref
        assert body_file.is_file()
        assert body_file.read_text(encoding="utf-8") == bodies_by_id[tool_call_id], (
            f"the file the JOIN reaches for {tool_call_id!r} must be the "
            "SAME call's own body, not the other call's"
        )


@pytest.mark.asyncio
async def test_a_caller_with_no_tool_call_id_still_produces_a_valid_history_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: accept-side control -- a dispatch with NO `tool_call_id`
    (e.g. a CodeAct `tool()` call, modeled here by simply not threading
    one through) must not break the history side at all; the absence is
    an audit-event-only distinction (`RouterLoop.feedback`'s own history
    row always carries `tc["id"]` regardless of what the audit event
    recorded, since feedback() reads the `tc` dict directly, not the
    audit trail)."""
    monkeypatch.chdir(tmp_path)
    session = _session("no-id-agent", tmp_path)
    collected = collect_events(session)

    tc = {"id": "call_only", "type": "function", "function": {"name": "mcp", "arguments": "{}"}}

    async def _invoker(args: dict) -> dict:
        return _mcp_content(_BODY_1)

    ctx = DispatchContext(
        caller_kind="operator", caller_id=session.agent_name, chain_id=None,
        tool_catalog={"mcp": {"function": {"name": "mcp"}}},
        events=session.router_host.events, contextual=None,
        # tool_call_id deliberately omitted -- an /exec-shaped caller.
    )
    result = await dispatch_tool(name="mcp", args={}, ctx=ctx, invoker=_invoker)
    result.setdefault("_canonical_source", "mcp")

    await settle(session)
    (returned,) = [e for e in collected if e.type == "tool_returned"]
    assert returned.data["tool_call_id"] is None
    assert returned.data["tool_call_id_absent_reason"] == "operator"

    from reyn.tools.scheme import ExecutionResult

    loop = RouterLoop(host=session.router_host, chain_id="c1", router_model=_MODEL)
    loop.feedback(ExecutionResult(tool_calls=[tc], tool_results=[result], assistant_content=""))
    await loop.persist_feedback()

    (row,) = [m for m in session.history if m.role == "tool"]
    assert row.tool_call_id == "call_only", (
        "the history row's own tool_call_id comes from tc['id'] directly "
        "-- the audit event's absence does not propagate to it"
    )
    assert row.meta.get(CONTENT_REF_META_KEY)


@pytest.mark.asyncio
async def test_execute_tool_seam_threads_tcs_own_id_through_to_the_audit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: LOAD-BEARING — the ACTUAL production wiring
    (`RouterLoop._execute_tool` -> `_dispatch_resolved` ->
    `DispatchContext(tool_call_id=...)`), not a hand-built
    `DispatchContext` the way the tests above construct one directly.
    Uses `_execute_tool`, the documented test-only seam for exactly this
    (no production call site duplicates it, per that method's own
    docstring), with `self._catalog` populated and `_invoke_router_tool`
    overridden on the instance -- the two things a real `run()` turn
    would otherwise populate via an LLM call this test has no need to
    drive.

    strip: remove `tool_call_id=tc.get("id")` from `_execute_tool`'s own
    `_dispatch_resolved(...)` call -- the emitted event's `tool_call_id`
    reverts to `None` even though `tc["id"]` was real, the exact
    "wiring silently dropped between two correct halves" shape a
    hand-built-context test can never catch."""
    monkeypatch.chdir(tmp_path)
    session = _session("wiring-agent", tmp_path)
    collected = collect_events(session)

    loop = RouterLoop(host=session.router_host, chain_id="c1", router_model=_MODEL)
    loop._catalog = {"echo": {"function": {"name": "echo"}}}

    async def _stub_invoker(name: str, args: dict) -> dict:
        return {"echoed": args}

    monkeypatch.setattr(loop, "_invoke_router_tool", _stub_invoker)

    tc = {"id": "call_from_tc", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
    result = await loop._execute_tool(tc, call_id="round_x")
    assert result["status"] == "ok"
    assert result["data"] == {"echoed": {}}

    await settle(session)
    (returned,) = [e for e in collected if e.type == "tool_returned"]
    assert returned.data["tool_call_id"] == "call_from_tc", (
        "the id must reach the audit event via the REAL _execute_tool -> "
        "_dispatch_resolved -> DispatchContext chain, not merely be "
        "constructible by hand"
    )
    assert returned.data["call_id"] == "round_x"
