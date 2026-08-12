"""#4368 (arc #4412) -- the single seam every reyn-side production
``lowlevel.Server`` construction goes through.

Before this module, 6 sites across 2 files (`server.py` construction +
`interfaces/web/routers/mcp.py`'s SSE transport) each imported directly from
`mcp.server.*` and registered handlers via that SDK line's own decorator
API (`@server.list_tools()`/`@server.call_tool()`/`@server.read_resource()`
-- gone entirely on mcp 2.0, replaced by `Server(...)` constructor kwargs,
measured live against a real `mcp==2.0.0` install, not assumed). A future
pin bump has to change how registration happens either way; this module
makes it ONE seam for the `Server` CONSTRUCTION axis specifically -- the
swap edits THIS file's function body, not every construction call site.

**This seam's scope is construction only -- 3 other `mcp.server.*` surfaces
are OUTSIDE it, and their mcp 2.0 shape is UNMEASURED** (architect co-vet,
#4368, live archaeology after a first-pass `git grep -E "^\\s*from
mcp\\.server"` silently matched nothing -- POSIX ERE doesn't understand
`\\s`, so a 0-hit census read as "no imports outside the seam" when it was
actually "the pattern never fired"; re-run with `[[:space:]]` found these):

```
interfaces/web/routers/mcp.py:60   from mcp.server.sse import SseServerTransport
src/reyn/mcp/server.py:1042        from mcp.server import NotificationOptions
src/reyn/mcp/server.py:1087        from mcp.server.stdio import stdio_server
```

A pin-bump PR reading only the paragraph above would wrongly conclude "edit
this one file's function bodies and the swap is done" -- these 3 sites
still need their own pin-bump pass, whatever mcp 2.0 turns out to require
of them (not yet measured, not assumed broken). Mirrors
`_fastmcp_boundary.py`'s own pattern from #3698 P2 (the CLIENT-side
equivalent, since deleted once its own swap completed -- #4282/#4299) —
architect's explicit precedent for this ruling (#4368).

## What this seam covers, and what it deliberately does NOT

`build_mcp_server`'s ``on_list_tools``/``on_call_tool``/``on_read_resource``
parameters use mcp 2.0's real handler REGISTRATION signature directly:
``async def handler(ctx, params) -> Result`` (``ListToolsResult``/
``CallToolResult``/``ReadResourceResult``), the same shape
``mcp.server.lowlevel.Server.__init__``'s own ``on_list_tools=``/etc.
kwargs accept on 2.0 (verified live). Under the CURRENT pin
(`mcp>=1.24,<2.0`), this function adapts each 2.0-shaped handler onto the
1.x line's decorator API internally; once the pin bumps, this function's
body collapses to a near-passthrough (`Server(name,
on_list_tools=on_list_tools, ...)` directly) and the swap is done.

**Object CONSTRUCTION inside a handler body (`Tool(...)`,
`TextResourceContents(...)`, …) is a separate axis this seam does NOT
cover** (owner ruling via lead-coder, #4368: a per-SDK-type constructor
function here would mean reyn's own surface grows every time the SDK adds
a type -- not reyn's responsibility to own, same discriminator as #4354's
provider-layer ruling). Handler bodies write those constructions plain,
in the CURRENTLY INSTALLED pin's own vocabulary (`inputSchema`/`mimeType`
today); a pin-bump PR flips every such call site in one mechanical pass
alongside this module. This is why :func:`_read_resource`'s adapter below
reads `c.mimeType`, not `c.mime_type` -- it has to track whichever
vocabulary the handler bodies currently use, and moves in the SAME
pin-bump PR as they do.

## The ``ctx`` adaptation this seam owns

mcp 2.0's ``on_call_tool``/``on_read_resource`` receive a
``ServerRequestContext`` directly as their first argument. The 1.x
decorator API has no such parameter -- the equivalent data was reached via
``server.request_context`` (a property lookup on the ``Server`` instance
itself), which this seam still uses internally under the current pin,
wrapped in a small adapter (:class:`_CtxAdapter`) so the handler body sees
the SAME ``.session``/``.request_id``/``.meta`` shape regardless of pin.

The one genuine shape difference this seam has to paper over: ``.meta``.
On the 1.x line it is ``mcp.types.RequestParams.Meta``, a real pydantic
``BaseModel`` (attribute access, camelCase field names --
``.progressToken``). On 2.0 it is confirmed live to be a real
``TypedDict`` (plain ``dict`` at runtime, snake_case keys --
``.get("progress_token")``). Handler bodies are written against the 2.0
shape (dict-style ``.get(...)``, snake_case), so :class:`_CtxAdapter`
normalises a 1.x pydantic ``Meta`` into a plain dict with the SAME
snake_case keys at adaptation time -- one conversion, in one place, rather
than every handler needing its own version branch.

## Why the accessors are functions, not module-level re-exports

Same reasoning as `_fastmcp_boundary.py`: the ``mcp`` import stays deferred
inside the function body (not at module top), so a test environment that
never touches the MCP surface never pays the import cost, and any
``ImportError`` fires at the same point it always did (first real use, not
at ``reyn.mcp`` import time).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

_Handler = Callable[[Any, Any], Awaitable[Any]]


class _CtxAdapter:
    """Normalises 1.x's ``server.request_context`` (a property lookup) into
    the same ``.session``/``.request_id``/``.meta`` shape mcp 2.0 hands
    ``on_call_tool``/``on_read_resource`` directly as their first argument
    -- see this module's own docstring, "The ``ctx`` adaptation this seam
    owns", for the full rationale."""

    def __init__(self, raw: "Any") -> None:
        self.session = raw.session
        self.request_id = raw.request_id
        self.meta = _adapt_meta(raw.meta)


def _adapt_meta(meta: "Any") -> "dict[str, Any] | None":
    """1.x's ``RequestParams.Meta`` (pydantic ``BaseModel``, camelCase
    attributes) -> a plain dict with 2.0's snake_case keys, so handler
    bodies written against 2.0's real ``TypedDict``/dict shape
    (``ctx.meta.get("progress_token")``) work unchanged under either pin.
    Only the one field reyn's own handlers actually read is mapped --
    extend this if a future handler needs another ``_meta`` key."""
    if meta is None:
        return None
    if isinstance(meta, dict):
        return meta  # already 2.0-shaped (or a caller-constructed dict in a test)
    return {"progress_token": getattr(meta, "progressToken", None)}


def build_mcp_server(
    name: str,
    *,
    on_list_tools: "_Handler | None" = None,
    on_call_tool: "_Handler | None" = None,
    on_read_resource: "_Handler | None" = None,
) -> "Any":
    """Construct a ``lowlevel.Server`` the CURRENT pin's way, from handlers
    already written in mcp 2.0's own ``(ctx, params) -> Result`` shape --
    see this module's own docstring for the full rationale."""
    from mcp.server import Server

    server = Server(name)

    if on_list_tools is not None:
        @server.list_tools()  # type: ignore[misc]
        async def _list_tools() -> "list[Any]":
            result = await on_list_tools(_CtxAdapter(server.request_context), None)  # type: ignore[attr-defined]
            return result.tools

    if on_call_tool is not None:
        @server.call_tool()  # type: ignore[misc]
        async def _call_tool(tool_name: str, arguments: dict) -> "list[Any]":
            from mcp.types import CallToolRequestParams

            params = CallToolRequestParams(name=tool_name, arguments=arguments)
            result = await on_call_tool(_CtxAdapter(server.request_context), params)  # type: ignore[attr-defined]
            return result.content

    if on_read_resource is not None:
        @server.read_resource()  # type: ignore[misc]
        async def _read_resource(uri: "Any") -> "list[Any]":
            from mcp.server.lowlevel.helper_types import ReadResourceContents
            from mcp.types import ReadResourceRequestParams

            params = ReadResourceRequestParams(uri=str(uri))  # type: ignore[arg-type]
            result = await on_read_resource(_CtxAdapter(server.request_context), params)  # type: ignore[attr-defined]
            return [
                ReadResourceContents(content=c.text, mime_type=c.mimeType)
                for c in result.contents
            ]

    return server
