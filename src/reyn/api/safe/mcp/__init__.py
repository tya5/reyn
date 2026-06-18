"""MCP-related helpers callable from safe-mode python steps.

Currently exposes:

- :mod:`reyn.api.safe.mcp.registry` — MCP server registry lookup
  (= ``search`` / ``lookup``). Hardcoded URL, no permission gate.

The naming ``reyn.api.safe.mcp.*`` mirrors the wider ``reyn.api.safe.*`` doctrine
(= per-call gated where state mutation is involved; ambient where the call
is observation-only). Registry lookup is ambient — see the module docstring
for the threat model.
"""

from . import registry

__all__ = ["registry"]
