"""#4368 (arc #4412) -- the single seam every reyn-side production
``lowlevel.Server`` construction goes through.

#4412 pin-bump PR: mcp is now pinned `>=2.0,<3.0`. This module was built
under the 1.x line specifically so the registration/ctx-adaptation swap for
the eventual 2.0 bump would touch ONE file, not every construction call
site (#4368) -- that promise is now redeemed: `build_mcp_server` collapsed
to a near-passthrough, and the 1.x-only `_CtxAdapter`/`_adapt_meta` classes
(which used to normalise `server.request_context`'s pydantic `Meta` into
2.0's plain-dict shape) are gone entirely, because 2.0 hands `ctx` to a
registered handler directly -- there is nothing left to adapt.

**This seam's scope was always construction only -- 3 other `mcp.server.*`
surfaces were OUTSIDE it** (architect co-vet, #4368):

```
interfaces/web/routers/mcp.py:60   from mcp.server.sse import SseServerTransport
src/reyn/mcp/server.py:1042        from mcp.server import NotificationOptions
src/reyn/mcp/server.py:1087        from mcp.server.stdio import stdio_server
```

Confirmed live against `mcp==2.0.0`, this bump: none of the 3 needed a code
change -- `SseServerTransport`/`NotificationOptions`/`stdio_server` all
still live at those exact import paths, unchanged shape. Recorded here
because the #4368 docstring explicitly asked this bump to report back on
them, not because anything broke.

## What this seam covers, and what it deliberately does NOT

`build_mcp_server`'s ``on_list_tools``/``on_call_tool``/``on_read_resource``
parameters use mcp 2.0's real handler REGISTRATION signature directly:
``async def handler(ctx, params) -> Result`` (``ListToolsResult``/
``CallToolResult``/``ReadResourceResult``) -- the exact same shape
``mcp.server.lowlevel.Server.__init__``'s own ``on_list_tools=``/etc.
kwargs accept, so this function is now a straight passthrough onto them.

**Object CONSTRUCTION inside a handler body (`Tool(...)`,
`TextResourceContents(...)`, …) was a separate axis this seam never
covered** (owner ruling via lead-coder, #4368: a per-SDK-type constructor
function here would mean reyn's own surface grows every time the SDK adds
a type -- not reyn's responsibility to own, same discriminator as #4354's
provider-layer ruling). Handler bodies write those constructions plain, in
mcp 2.0's own vocabulary now (`input_schema`/`mime_type`, flipped from
`inputSchema`/`mimeType` in this same pin-bump commit, mechanically,
alongside this module).

## Why the accessor stays a function, not a module-level re-export

Same reasoning as `_fastmcp_boundary.py` had: the ``mcp`` import stays
deferred inside the function body (not at module top), so a test
environment that never touches the MCP surface never pays the import
cost, and any ``ImportError`` fires at the same point it always did (first
real use, not at ``reyn.mcp`` import time).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

_Handler = Callable[[Any, Any], Awaitable[Any]]


def build_mcp_server(
    name: str,
    *,
    on_list_tools: "_Handler | None" = None,
    on_call_tool: "_Handler | None" = None,
    on_read_resource: "_Handler | None" = None,
) -> "Any":
    """Construct a ``lowlevel.Server`` from handlers already written in mcp
    2.0's own ``(ctx, params) -> Result`` shape.

    #4412 pin-bump PR: this function is now the near-passthrough this
    module's own docstring predicted it would collapse to -- ``ctx`` is
    handed to a registered handler directly by ``Server.__init__``'s
    ``on_list_tools=``/etc. kwargs on 2.0 (verified live), so the
    ``_CtxAdapter``/``server.request_context`` property-lookup dance this
    function used to do for the 1.x line is gone. This is exactly the
    "swap edits THIS file's function body, not every call site" promise
    the seam existed to keep."""
    from mcp.server import Server

    return Server(
        name,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_read_resource=on_read_resource,
    )
