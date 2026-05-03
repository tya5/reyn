"""Tests for src/reyn/dispatch/dispatcher.py."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from reyn.dispatch import (
    DispatchContext,
    dispatch_tool,
    UnknownToolError,
    InvalidArgsError,
)


class FakeEventEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **data: Any) -> None:
        self.events.append((event_type, data))


def make_ctx(
    *,
    caller_kind: str = "router",
    caller_id: str = "test_agent",
    chain_id: str | None = "c1",
    catalog: dict | None = None,
    events: FakeEventEmitter | None = None,
) -> tuple[DispatchContext, FakeEventEmitter]:
    e = events or FakeEventEmitter()
    return (
        DispatchContext(
            caller_kind=caller_kind,
            caller_id=caller_id,
            chain_id=chain_id,
            tool_catalog=catalog or {},
            events=e,
        ),
        e,
    )


_SAMPLE_CATALOG = {
    "list_skills": {
        "function": {
            "name": "list_skills",
            "description": "List skills",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    },
}


def test_unknown_tool_returns_error_kind_and_emits_failed_event():
    async def main():
        ctx, ev = make_ctx(catalog=_SAMPLE_CATALOG)
        result = await dispatch_tool(
            name="bogus", args={}, ctx=ctx,
            invoker=_unused_invoker,
        )
        assert result == {"status": "error",
                          "error": {"kind": "unknown_tool",
                                    "message": "Tool 'bogus' not in catalog"}}
        # Failed event emitted, no called/returned events
        types = [e[0] for e in ev.events]
        assert "tool_failed" in types
        assert "tool_called" not in types
        assert "tool_returned" not in types
    asyncio.run(main())


def test_invalid_args_returns_error_kind():
    async def main():
        ctx, ev = make_ctx(catalog=_SAMPLE_CATALOG)
        result = await dispatch_tool(
            name="list_skills", args={"wrong": "no path"},
            ctx=ctx, invoker=_unused_invoker,
        )
        assert result["status"] == "error"
        assert result["error"]["kind"] == "invalid_args"
        # invoker not called
    asyncio.run(main())


def test_happy_path_emits_called_then_returned():
    async def main():
        async def invoker(args):
            return {"items": []}
        ctx, ev = make_ctx(catalog=_SAMPLE_CATALOG)
        result = await dispatch_tool(
            name="list_skills", args={"path": ""},
            ctx=ctx, invoker=invoker,
        )
        assert result == {"status": "ok", "data": {"items": []}}
        types = [e[0] for e in ev.events]
        assert types == ["tool_called", "tool_returned"]
        # Pre-event includes args + args_hash (skill resume design,
        # PR-step-events: args_keys → args + hash for replay memoization)
        called_data = ev.events[0][1]
        assert called_data["args"] == {"path": ""}
        assert called_data["args_hash"]  # non-empty
        assert called_data["caller_kind"] == "router"
        assert called_data["caller_id"] == "test_agent"
        assert called_data["chain_id"] == "c1"
        assert called_data["tool"] == "list_skills"
        # Post-event includes result + args_hash for replay memoization.
        returned_data = ev.events[1][1]
        assert returned_data["result"] == {"items": []}
        assert returned_data["args_hash"] == called_data["args_hash"]
    asyncio.run(main())


def test_permission_error_inside_invoker_returns_permission_denied():
    async def main():
        async def invoker(args):
            raise PermissionError("nope")
        ctx, ev = make_ctx(catalog=_SAMPLE_CATALOG)
        result = await dispatch_tool(
            name="list_skills", args={"path": ""},
            ctx=ctx, invoker=invoker,
        )
        assert result["status"] == "error"
        assert result["error"]["kind"] == "permission_denied"
        assert "nope" in result["error"]["message"]
    asyncio.run(main())


def test_generic_exception_inside_invoker_returns_exception_kind():
    async def main():
        async def invoker(args):
            raise ValueError("boom")
        ctx, ev = make_ctx(catalog=_SAMPLE_CATALOG)
        result = await dispatch_tool(
            name="list_skills", args={"path": ""},
            ctx=ctx, invoker=invoker,
        )
        assert result["status"] == "error"
        assert result["error"]["kind"] == "exception"
        assert "ValueError" in result["error"]["message"]
        assert "boom" in result["error"]["message"]
    asyncio.run(main())


def test_no_schema_skips_arg_validation():
    """If the tool definition has no parameters schema, skip arg validation."""
    catalog = {
        "free_form": {"function": {"name": "free_form", "description": "no schema"}}
    }
    async def main():
        async def invoker(args):
            return args
        ctx, ev = make_ctx(catalog=catalog)
        result = await dispatch_tool(
            name="free_form", args={"anything": 123},
            ctx=ctx, invoker=invoker,
        )
        assert result["status"] == "ok"
        assert result["data"] == {"anything": 123}
    asyncio.run(main())


def test_caller_kind_and_id_propagated_in_events():
    async def main():
        async def invoker(args):
            return None
        ctx, ev = make_ctx(
            caller_kind="skill_phase",
            caller_id="article_writer.write",
            chain_id="cabc",
            catalog=_SAMPLE_CATALOG,
        )
        await dispatch_tool(
            name="list_skills", args={"path": "x"},
            ctx=ctx, invoker=invoker,
        )
        for ev_type, ev_data in ev.events:
            assert ev_data["caller_kind"] == "skill_phase"
            assert ev_data["caller_id"] == "article_writer.write"
            assert ev_data["chain_id"] == "cabc"
    asyncio.run(main())


async def _unused_invoker(args):
    raise AssertionError("invoker should not be called")
