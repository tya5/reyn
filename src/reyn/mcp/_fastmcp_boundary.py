"""#3698 P2 — the single seam every reyn-side `fastmcp` import goes through.

Before this module, 6 sites across 2 files (`client.py` ×5, `elicitation.py`
×1) each imported directly from `fastmcp.*`. A future SDK swap (the MCP
Python SDK v2 target, #3698) had to find and change every one of those call
sites individually. This module makes them one seam — a future swap edits
THIS file's function bodies, not 6 scattered call sites.

## Why the accessors are functions, not module-level re-exports

Every site here was a DEFERRED import (inside a method body, not at module
top) before this change — `client.py`'s own first import is wrapped in a
`try/except ImportError` that raises a friendly reyn-specific error ("The
'fastmcp' package is required...") rather than a bare traceback. Turning
these into a plain module-level `from fastmcp import Client` here would
change WHEN the import (and any ImportError) fires — from "the first time a
connection is actually opened" to "the first time `reyn.mcp` is imported at
all", independent of whether MCP is ever used in a given run. Each accessor
function below preserves the original timing exactly: the import inside it
only executes when the caller actually calls the function, same as the
inline import it replaces.

## History: message_handler.py's 2 sites (#3698 P3 removed the need for this)

This module used to ALSO re-export `MessageHandler`/`TaskNotificationHandler`
for `message_handler.py`'s `ReynMCPMessageHandler(TaskNotificationHandler)`
— an INHERITANCE dependency, not a plain import, so the re-export only
relocated the import path without removing the coupling (explicitly marked
as such at the time, both here and at `message_handler.py`'s own import
site). #3698 P3 (see `message_handler.py`'s module docstring) measured the
ACTUAL fastmcp/mcp call contract by reading the installed source: the MCP
SDK invokes the message handler as a plain `Callable` (`MessageHandlerFnT`,
a `Protocol`) — no inheritance is required anywhere in the real call chain.
P3 rewrote `ReynMCPMessageHandler` to compose rather than inherit, which
means it no longer imports anything from `fastmcp` at all (only
`mcp.types`, the lower-level protocol-spec package fastmcp itself wraps) —
the relocation-only re-export this section used to describe no longer has a
consumer, so it was deleted in the same PR that removed the coupling it
existed to (honestly) describe.
"""
from __future__ import annotations

from typing import Any


def import_fastmcp_client() -> "type[Any]":
    """``fastmcp.Client`` — client.py's ``initialize()``, wrapped there in a
    try/except that raises a reyn-specific "package required" error on
    ``ImportError`` (unchanged by this function; the caller still wraps
    THIS call the same way)."""
    from fastmcp import Client

    return Client


def import_stdio_transport() -> "type[Any]":
    """``fastmcp.client.transports.StdioTransport`` — client.py's ``_open_stdio``."""
    from fastmcp.client.transports import StdioTransport

    return StdioTransport


def import_streamable_http_transport() -> "type[Any]":
    """``fastmcp.client.transports.StreamableHttpTransport`` — client.py's
    ``_open_http``."""
    from fastmcp.client.transports import StreamableHttpTransport

    return StreamableHttpTransport


def import_sse_transport() -> "type[Any]":
    """``fastmcp.client.transports.SSETransport`` — client.py's ``_open_sse``."""
    from fastmcp.client.transports import SSETransport

    return SSETransport


def import_oauth() -> "type[Any]":
    """``fastmcp.client.auth.OAuth`` — client.py's ``_build_oauth_auth``."""
    from fastmcp.client.auth import OAuth

    return OAuth


def import_elicit_result() -> "type[Any]":
    """``fastmcp.client.elicitation.ElicitResult`` — elicitation.py's
    ``build_elicitation_handler``."""
    from fastmcp.client.elicitation import ElicitResult

    return ElicitResult
