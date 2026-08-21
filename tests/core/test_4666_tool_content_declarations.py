"""Tier 1/2: #4666 item ③b — a tool declares which of its args/result
fields are conversation content (`reyn.core.dispatch.content_declarations`);
``dispatch_tool`` consults that declaration and applies ②'s
(``completed_response_include_text``) or ③'s (``user_input_include_text``)
knob to ``tool_called.args`` / ``tool_returned.result`` accordingly.

Closes the gap #4970's review found: ``ask_user``'s question/answer used
to reach the audit log unconditionally through the GENERIC ``tool_called``/
``tool_returned`` kinds, bypassing ②③ entirely (those knobs only governed
the DEDICATED user_intervention_requested/received kinds). Architect's
ruling explicitly rejected a 4th knob (owner's 3 are keyed by content
CLASS, a 4th keyed by carrier would split the vocabulary onto two axes)
in favor of a per-tool declaration the generic dispatcher consults.

Real `dispatch_tool` + `DispatchContext` throughout (mirrors
tests/core/test_dispatcher.py's own fixtures/asyncio.run convention), no
unittest.mock. Declares and un-declares a throwaway tool name per test
(never touches the real ``ask_user`` registration) so tests are
independent of import order.
"""
from __future__ import annotations

import asyncio
from typing import Any

# Importing this module is what makes ask_user's own declare_content_fields
# call (a module-load-time side effect, mirroring this tree's existing
# `register(...)` idiom) actually run — a test process that never imports
# any tools/*.py module would otherwise see an empty registry regardless
# of production behavior. reyn.tools.ask_user unused directly; the import
# itself is the fixture.
import reyn.tools.ask_user  # noqa: F401
from reyn.core.dispatch import DispatchContext, dispatch_tool
from reyn.core.dispatch.content_declarations import (
    _TOOL_CONTENT_FIELDS,
    declare_content_fields,
    declared_tools,
    get_content_fields,
)


class FakeEventEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **data: Any) -> None:
        self.events.append((event_type, data))

    def find(self, event_type: str) -> dict:
        return next(data for etype, data in self.events if etype == event_type)


_CATALOG = {
    "probe_tool": {
        "function": {
            "name": "probe_tool",
            "description": "test-only tool",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "other": {"type": "string"},
                },
            },
        },
    },
}


def _reset_probe_tool_declaration() -> None:
    _TOOL_CONTENT_FIELDS.pop("probe_tool", None)


async def _handler_echo(args: dict) -> dict:
    return {"question": args.get("question", ""), "answer": "the user's answer"}


def _make_ctx(
    *, completed_response_include_text: bool = False,
    user_input_include_text: bool = False,
) -> tuple[DispatchContext, FakeEventEmitter]:
    events = FakeEventEmitter()
    ctx = DispatchContext(
        caller_kind="router",
        caller_id="test_agent",
        chain_id="c1",
        tool_catalog=_CATALOG,
        events=events,
        completed_response_include_text=completed_response_include_text,
        user_input_include_text=user_input_include_text,
    )
    return ctx, events


# ── the registry itself ─────────────────────────────────────────────────


def test_declare_then_get_round_trips():
    """Tier 1: a declared tool's fields are retrievable exactly as declared."""
    _reset_probe_tool_declaration()
    try:
        declare_content_fields("probe_tool", {"question": "assistant", "answer": "user"})
        assert dict(get_content_fields("probe_tool")) == {
            "question": "assistant", "answer": "user",
        }
    finally:
        _reset_probe_tool_declaration()


def test_an_undeclared_tool_returns_empty():
    """Tier 1: a tool that never called declare_content_fields has no
    declared fields — the default for every tool except ask_user."""
    assert dict(get_content_fields("some_never_declared_tool_xyz")) == {}


def test_declared_tools_names_ask_user_and_nothing_else():
    """Tier 1: #4666's own bound — the set of tools with a NON-EMPTY
    declaration is exactly {"ask_user"} today. A 2nd tool showing up here
    is a deliberate, reviewed addition (architect's ruling: reconsider the
    whole shape when that happens) — this test intentionally fails the
    moment that occurs, mirroring this tree's `_force_inline` bound."""
    _reset_probe_tool_declaration()
    assert declared_tools() == frozenset({"ask_user"})


# ── dispatch_tool applies the declared knob per field ───────────────────


def test_tool_called_args_follow_completed_response_knob_off():
    """Tier 2: probe_tool.question is declared "assistant" (②'s class) —
    tool_called.args drops it when completed_response_include_text is off."""
    async def main():
        _reset_probe_tool_declaration()
        declare_content_fields("probe_tool", {"question": "assistant"})
        try:
            ctx, events = _make_ctx(completed_response_include_text=False)
            await dispatch_tool(
                name="probe_tool", args={"question": "what next?", "other": "kept"},
                ctx=ctx, invoker=_handler_echo,
            )
            called_args = events.find("tool_called")["args"]
            assert "question" not in called_args
            assert called_args["other"] == "kept"
        finally:
            _reset_probe_tool_declaration()
    asyncio.run(main())


def test_tool_called_args_follow_completed_response_knob_on():
    """Tier 2: same as above, opt-in direction — the field survives."""
    async def main():
        _reset_probe_tool_declaration()
        declare_content_fields("probe_tool", {"question": "assistant"})
        try:
            ctx, events = _make_ctx(completed_response_include_text=True)
            await dispatch_tool(
                name="probe_tool", args={"question": "what next?", "other": "kept"},
                ctx=ctx, invoker=_handler_echo,
            )
            called_args = events.find("tool_called")["args"]
            assert called_args["question"] == "what next?"
            assert called_args["other"] == "kept"
        finally:
            _reset_probe_tool_declaration()
    asyncio.run(main())


def test_tool_returned_result_follows_user_input_knob_off():
    """Tier 2: probe_tool's result "answer" field is declared "user" (③'s
    class) — tool_returned.result drops it when user_input_include_text
    is off."""
    async def main():
        _reset_probe_tool_declaration()
        declare_content_fields("probe_tool", {"answer": "user"})
        try:
            ctx, events = _make_ctx(user_input_include_text=False)
            await dispatch_tool(
                name="probe_tool", args={"question": "q"}, ctx=ctx, invoker=_handler_echo,
            )
            returned_result = events.find("tool_returned")["result"]
            assert "answer" not in returned_result
            assert returned_result["question"] == "q"
        finally:
            _reset_probe_tool_declaration()
    asyncio.run(main())


def test_tool_returned_result_follows_user_input_knob_on():
    """Tier 2: same as above, opt-in direction — the field survives."""
    async def main():
        _reset_probe_tool_declaration()
        declare_content_fields("probe_tool", {"answer": "user"})
        try:
            ctx, events = _make_ctx(user_input_include_text=True)
            await dispatch_tool(
                name="probe_tool", args={"question": "q"}, ctx=ctx, invoker=_handler_echo,
            )
            returned_result = events.find("tool_returned")["result"]
            assert returned_result["answer"] == "the user's answer"
        finally:
            _reset_probe_tool_declaration()
    asyncio.run(main())


def test_the_two_knobs_are_independent_per_field():
    """Tier 2: architect's corrected ruling — question (②) and answer (③)
    are governed by SEPARATE knobs; one on and the other off is a valid,
    deliberate operator choice, not a defect. Verifies both directions in
    one call so a coupling regression (either knob accidentally gating
    both fields) would be caught."""
    async def main():
        _reset_probe_tool_declaration()
        declare_content_fields("probe_tool", {"question": "assistant", "answer": "user"})
        try:
            ctx, events = _make_ctx(
                completed_response_include_text=True, user_input_include_text=False,
            )
            await dispatch_tool(
                name="probe_tool", args={"question": "q"}, ctx=ctx, invoker=_handler_echo,
            )
            assert "question" in events.find("tool_called")["args"]
            assert "answer" not in events.find("tool_returned")["result"]
        finally:
            _reset_probe_tool_declaration()
    asyncio.run(main())


def test_an_undeclared_tool_is_never_touched():
    """Tier 1: a tool with NO declaration passes args/result through
    completely unchanged (not even copied) regardless of either knob —
    this mechanism is strictly additive, opt-in per tool."""
    async def main():
        _reset_probe_tool_declaration()
        ctx, events = _make_ctx(
            completed_response_include_text=False, user_input_include_text=False,
        )
        original_args = {"question": "q", "other": "kept"}
        await dispatch_tool(
            name="probe_tool", args=original_args, ctx=ctx, invoker=_handler_echo,
        )
        assert events.find("tool_called")["args"] is original_args
        assert "answer" in events.find("tool_returned")["result"]
    asyncio.run(main())


def test_the_returned_data_to_the_caller_is_never_redacted():
    """Tier 2: redaction applies ONLY to the copy that reaches the audit
    event — the value dispatch_tool hands back to the caller/LLM (used to
    build the next turn's context) must keep every field regardless of
    either knob, or the model would lose its own tool result."""
    async def main():
        _reset_probe_tool_declaration()
        declare_content_fields("probe_tool", {"question": "assistant", "answer": "user"})
        try:
            ctx, _events = _make_ctx(
                completed_response_include_text=False, user_input_include_text=False,
            )
            outcome = await dispatch_tool(
                name="probe_tool", args={"question": "q"}, ctx=ctx, invoker=_handler_echo,
            )
            assert outcome["status"] == "ok"
            assert outcome["data"]["answer"] == "the user's answer"
        finally:
            _reset_probe_tool_declaration()
    asyncio.run(main())


def test_args_hash_is_unaffected_by_redaction():
    """Tier 1: args_hash is a correlation fingerprint over the FULL,
    unredacted args (dispatch_tool's own docstring: "a fingerprint ...
    not a value substitute") — it must be identical whether the declared
    field is redacted or not, so a consumer can still correlate a
    redacted tool_called with its tool_returned by hash."""
    async def main():
        _reset_probe_tool_declaration()
        declare_content_fields("probe_tool", {"question": "assistant"})
        try:
            ctx_off, events_off = _make_ctx(completed_response_include_text=False)
            ctx_on, events_on = _make_ctx(completed_response_include_text=True)
            await dispatch_tool(
                name="probe_tool", args={"question": "q"}, ctx=ctx_off, invoker=_handler_echo,
            )
            await dispatch_tool(
                name="probe_tool", args={"question": "q"}, ctx=ctx_on, invoker=_handler_echo,
            )
            assert (
                events_off.find("tool_called")["args_hash"]
                == events_on.find("tool_called")["args_hash"]
            )
        finally:
            _reset_probe_tool_declaration()
    asyncio.run(main())
