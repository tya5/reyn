"""#4368 (arc #4412) -- the single seam every reyn-side field/call-shape
read that differs between the mcp SDK's 1.x and 2.0 lines goes through.

Sibling to `_mcp_server_boundary.py` (the decorator-API axis) -- lead-coder's
ruling: "same discriminator, same strength" applies here too. A seam is not
a shim (a shim's death condition is "someone decides to fix it properly," a
human decision, so it rots); this is a permanent structure whose death
condition is the pin bump itself -- mechanical, so it doesn't rot. #4368's
own field renames (`client.py`'s `protocolVersion`/`nextCursor`/the
`list_tools()` call-shape change; `elicitation.py`'s `requestedSchema`)
block landing under the current pin exactly as hard as the server's
decorator-API removal does -- a DIFFERENT SDK mechanism (field renames, not
API removal), same landing consequence. This gap in the earlier "production
surface is small" measurement was found by a real wire round trip
(deliberately routed through mcp 1.x's own native client, bypassing
`client.py`, to isolate what the server seam alone actually proves) -- a
static count of `lowlevel.Server` construction sites had no way to surface
it, since `client.py` never touches `lowlevel.Server` at all.

## Each accessor is duck-typed on the ACTUAL object at runtime, not a
## version-string comparison

`getattr(obj, "protocol_version", None)` falling back to `getattr(obj,
"protocolVersion", None)` (etc.) works identically regardless of which pin
is actually installed -- no `mcp.__version__` check, no hardcoded version
branch. This is deliberate: the accessor stays correct even across a PATCH
release that might legitimately carry both names transiently (pydantic
alias support, confirmed live for some 2.0 fields during #4368's own
measurement work), and it makes the eventual pin-bump swap a pure
DELETION (drop the 1.x-named fallback branch), not a rewrite.

## Why the accessors are functions, not module-level constants

Same reasoning as `_fastmcp_boundary.py`/`_mcp_server_boundary.py`: no
version-branching import needed here at all (both pins expose these
objects at the same import paths -- only the FIELD/CALL shape differs),
but keeping the read behind a named function still means a future
pin-bump edits ONE function body per accessor, not every call site.
"""
from __future__ import annotations

import inspect
from typing import Any


def negotiated_protocol_version(init_result: "Any") -> str:
    """``InitializeResult.protocolVersion`` (1.x) / ``.protocol_version``
    (2.0, confirmed live) -- the version string the peer negotiated during
    the MCP handshake."""
    version = getattr(init_result, "protocol_version", None)
    if version is None:
        version = getattr(init_result, "protocolVersion", None)
    return str(version)


def next_page_cursor(list_result: "Any") -> "str | None":
    """``ListToolsResult``/``ListResourcesResult``/``ListPromptsResult``
    ``.nextCursor`` (1.x) / ``.next_cursor`` (2.0, confirmed live) -- the
    pagination cursor for the next page, or ``None`` on the last page."""
    if hasattr(list_result, "next_cursor"):
        return list_result.next_cursor
    return getattr(list_result, "nextCursor", None)


async def call_paginated_list(list_fn: "Any", cursor: "str | None") -> "Any":
    """Call a ``ClientSession.list_tools``/``list_resources``/``list_prompts``-
    shaped bound method with the CURRENT pin's actual call shape.

    1.x: a bare positional ``cursor: str | None``. 2.0 (confirmed live via
    ``inspect.signature``): keyword-only ``params: PaginatedRequestParams |
    None``, the positional shorthand removed entirely. Structural check
    (does the callable's own signature declare a ``params`` parameter),
    not a version-string comparison -- the same discriminator this
    module's own docstring names."""
    if "params" in inspect.signature(list_fn).parameters:
        from mcp.types import PaginatedRequestParams

        return await list_fn(params=PaginatedRequestParams(cursor=cursor))
    return await list_fn(cursor)


def requested_schema(elicit_params: "Any") -> "dict[str, Any]":
    """``ElicitRequestFormParams.requestedSchema`` (1.x) /
    ``.requested_schema`` (2.0, confirmed live) -- the raw JSON Schema dict
    for the elicitation's requested fields."""
    schema = getattr(elicit_params, "requested_schema", None)
    if schema is None:
        schema = getattr(elicit_params, "requestedSchema", None)
    return schema or {}
