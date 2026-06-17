"""Tests for src/reyn/dispatch/dispatcher.py."""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.core.dispatch import (
    DispatchContext,
    dispatch_tool,
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
    """Tier 2: unknown tool name returns error_kind=unknown_tool and emits tool_failed event."""
    async def main():
        ctx, ev = make_ctx(catalog=_SAMPLE_CATALOG)
        result = await dispatch_tool(
            name="bogus", args={}, ctx=ctx,
            invoker=_unused_invoker,
        )
        # #187 A: message names the unknown tool; no close match in this catalog
        # so no "Did you mean" hint is appended (kind/structure unchanged).
        assert result["status"] == "error"
        assert result["error"]["kind"] == "unknown_tool"
        assert "Tool 'bogus' not in catalog" in result["error"]["message"]
        assert "Did you mean" not in result["error"]["message"]
        # Failed event emitted, no called/returned events
        types = [e[0] for e in ev.events]
        assert "tool_failed" in types
        assert "tool_called" not in types
        assert "tool_returned" not in types
    asyncio.run(main())


def test_unknown_tool_suggests_close_match():
    """Tier 2: a near-miss tool name yields a 'Did you mean <X>?' hint (#187 A).

    The #187 dogfood saw the agent guess a non-catalog name (`source__grep`)
    from a real namespace cue, hit unknown_tool, and deterministically stop.
    The deny message now names the closest real catalog tool so the LLM can
    self-correct instead of stalling. The suggestion must be an actual catalog
    member (deny-message-decision-enabling).
    """
    async def main():
        catalog = {
            "source__read": {"function": {"name": "source__read"}},
            "source__list": {"function": {"name": "source__list"}},
            "exec__sandboxed_exec": {"function": {"name": "exec__sandboxed_exec"}},
        }
        ctx, _ev = make_ctx(catalog=catalog)
        result = await dispatch_tool(
            name="source__grep", args={}, ctx=ctx,
            invoker=_unused_invoker,
        )
        assert result["status"] == "error"
        assert result["error"]["kind"] == "unknown_tool"
        msg = result["error"]["message"]
        assert "Did you mean" in msg
        # The suggested name must be a real catalog member, not a fabrication.
        suggested = [name for name in catalog if repr(name) in msg]
        assert suggested, f"suggestion must name a catalog tool; got: {msg!r}"
    asyncio.run(main())


def test_invalid_args_returns_error_kind():
    """Tier 2: args failing schema validation return error_kind=invalid_args without calling invoker."""
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
    """Tier 2: successful dispatch emits tool_called then tool_returned events with correct fields."""
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
    """Tier 2: PermissionError raised by invoker maps to error_kind=permission_denied."""
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


def test_permission_denied_message_includes_actionable_hint():
    """Tier 2: permission_denied messages MUST include a "To allow:" hint.

    Without the hint, the user sees a bare PermissionError and has no path
    forward besides reading source. The hint is appended after the original
    message so substring checks ("nope" etc.) keep working downstream.
    """
    write_catalog = {
        "write_file": {
            "function": {
                "name": "write_file",
                "description": "Write a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        }
    }

    async def main():
        async def invoker(args):
            raise PermissionError("write to '/tmp/foo' was not approved.")
        ctx, ev = make_ctx(catalog=write_catalog)
        result = await dispatch_tool(
            name="write_file", args={"path": "/tmp/foo"},
            ctx=ctx, invoker=invoker,
        )
        msg = result["error"]["message"]
        # Original error stays as the prefix (substring contract).
        assert "write to '/tmp/foo' was not approved." in msg
        # Actionable hint is appended.
        assert "To allow:" in msg
        # The hint names the matching config key for the tool.
        assert "file.write" in msg
        # And points the user at a config file they can edit.
        assert "reyn.local.yaml" in msg

        # The same hint flows through to the tool_failed audit event so
        # later inspection (events tab / forensics) sees what the user saw.
        failed = [e for e in ev.events if e[0] == "tool_failed"]
        assert failed, "tool_failed event must fire on permission_denied"
        assert "To allow:" in failed[0][1]["message"]
    asyncio.run(main())


def test_permission_denied_unmapped_tool_falls_back_to_generic_hint():
    """Tier 2: unmapped tool names get a generic suffix, not a fabricated key.

    The fix must never invent a config key that doesn't exist; an unknown
    tool gets the "see the events tab" suffix instead.
    """
    catalog = {
        "some_unmapped_tool": {
            "function": {"name": "some_unmapped_tool", "description": "x"},
        }
    }

    async def main():
        async def invoker(args):
            raise PermissionError("nope")
        ctx, _ = make_ctx(catalog=catalog)
        result = await dispatch_tool(
            name="some_unmapped_tool", args={},
            ctx=ctx, invoker=invoker,
        )
        msg = result["error"]["message"]
        assert "nope" in msg
        assert "To allow:" in msg
        # Generic fallback — no fabricated config key.
        assert "events tab" in msg
    asyncio.run(main())


def test_generic_exception_inside_invoker_returns_exception_kind():
    """Tier 2: non-PermissionError raised by invoker maps to error_kind=exception with class and message."""
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
    """Tier 2: tool definition with no parameters schema skips arg validation and passes args through."""
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
    """Tier 2: caller_kind, caller_id, and chain_id from DispatchContext appear in all emitted events."""
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
