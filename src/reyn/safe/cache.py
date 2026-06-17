"""Safe-mode-callable shim over ``reyn.core.registry.cache`` (FP-0042 Phase 3 drift-fix).

Re-exports the existing file-based 24h TTL cache (= the same
``~/.reyn/registry-cache/<encoded_key>.json`` store the MCP / skill
registries already use) under the ``reyn.safe.*`` namespace so safe-mode
python steps can import it through the AST allowlist.

The underlying cache is unchanged — this module exists only to bridge
the import-path gap. ``reyn.core.registry.cache`` continues to host the
implementation; ``reyn.safe.cache`` is the safe-mode-visible alias.

Public API
----------

- :func:`get(key)` → ``dict | None`` — None on miss / expiry / corrupt.
- :func:`set(key, data)` — write (creates parent dirs automatically).

Threat-model note: the cache writes to ``~/.reyn/registry-cache/`` —
outside the per-skill ``permissions.file.write`` declaration. This is
intentional (= the cache is a cross-skill shared optimization, not
per-skill data) and consistent with how ``reyn.safe.mcp.registry``
already uses it internally.
"""
from __future__ import annotations

from reyn.core.registry.cache import get, set  # noqa: F401, A004 — intentional re-export

__all__ = ["get", "set"]
