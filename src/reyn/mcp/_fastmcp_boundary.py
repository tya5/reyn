"""#3698 P2 — the single seam every reyn-side `fastmcp` import goes through.

## What this closes, and what it does NOT close

Before this module, 8 sites across 3 files (`client.py` ×5, `elicitation.py`
×1, `message_handler.py` ×2) each imported directly from `fastmcp.*`. A
future SDK swap (the MCP Python SDK v2 target, #3698) had to find and
change every one of those call sites individually. This module makes them
one seam — a future swap edits THIS file's bodies, not 8 scattered call
sites.

**That claim is honest only for 6 of the 8 symbols.** `client.py`'s and
`elicitation.py`'s imports are plain symbol imports — construct-and-use,
never subclassed — so re-exporting them here is genuine, complete
decoupling: the CALLER never names `fastmcp` again. `message_handler.py`'s
`MessageHandler`/`TaskNotificationHandler` are different: `ReynMCPMessageHandler`
INHERITS from `TaskNotificationHandler` (see that module's own docstring),
depending on its `dispatch()` routing logic and its `_client_ref` attribute
contract — a real behavioral coupling, not just an import path. Re-exporting
the base class here relocates WHERE the name is imported from; it does
**not** remove that coupling. A future SDK swap still has to either find an
equivalent base class in the new SDK with the same routing contract, or
rewrite `ReynMCPMessageHandler` to compose rather than inherit (reimplementing
the dispatch routing reyn currently gets for free — tui-coder's #3698
measurement scoped this as the real Phase-3 cost). Re-exporting these two
here is for import-LOCATION consistency (one file names `fastmcp`, not
three) — it is deliberately NOT claimed as removing the inheritance
dependency, which stays exactly as coupled as before this module existed.

## Why the 6 real accessors are functions, not module-level re-exports

`client.py`'s 5 sites and `elicitation.py`'s 1 site were all DEFERRED
imports (inside a method body, not at module top) before this change —
`client.py`'s own first import is wrapped in a `try/except ImportError`
that raises a friendly reyn-specific error ("The 'fastmcp' package is
required...") rather than a bare traceback. Turning these into a plain
module-level `from fastmcp import Client` here would change WHEN the
import (and any ImportError) fires — from "the first time a connection is
actually opened" to "the first time `reyn.mcp` is imported at all",
independent of whether MCP is ever used in a given run. Each accessor
function below preserves the original timing exactly: the import inside it
only executes when the caller actually calls the function, same as the
inline import it replaces.

## Why `MessageHandler`/`TaskNotificationHandler` are plain re-exports, not functions

`message_handler.py`'s 2 sites were already module-level (not deferred) —
`class ReynMCPMessageHandler(TaskNotificationHandler):` needs its base class
resolved at class-definition (import) time, which Python cannot defer into
a function call. So these two stay module-level imports here too — no
timing change from before this module existed, in either direction.
"""
from __future__ import annotations

from typing import Any

# Module-level (see docstring): message_handler.py's class statement needs
# these resolved at import time, exactly as before this module existed.
from fastmcp.client.messages import MessageHandler as MessageHandler
from fastmcp.client.tasks import TaskNotificationHandler as TaskNotificationHandler


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
