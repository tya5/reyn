"""ExtraTool — a plugin-supplied MCP tool spec merged into build_server.

A gateway plugin's ``register_tools()`` returns a list of these; ``build_server``
merges them into the MCP server's catalog (``list_tools``) and dispatch
(``call_tool``). Kept free of the ``mcp`` SDK so plugins and the web loader can
import it without the SDK installed — ``build_server`` converts each to an SDK
``Tool`` internally.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class ExtraTool:
    """One plugin-provided MCP tool.

    ``handler`` is ``async (arguments: dict) -> str`` — it receives the parsed
    tool arguments and returns the tool's text result (build_server wraps it as
    an MCP ``TextContent``).
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Awaitable[str]]
