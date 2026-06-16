"""Back-compat shim (#1682): the MCP registry-lookup impl moved to
``reyn.mcp.registry``. ``reyn.safe.mcp.registry`` remains as the public allowlist
surface for skills (FP-0042) — mirroring how ``reyn.safe.cache`` re-exports
``reyn.registry.cache``. Repointing the allowlist + removing this shim is part of
the separate safe/ cleanup (#1).

This shim ALIASES the module (``sys.modules`` re-bind) rather than re-exporting
names, because the registry's HTTP layer (``_http_get_json``) is monkeypatched by
tests via ``import reyn.safe.mcp.registry as sr`` — a flat re-export would copy the
names, so a patch on the shim would not affect the real functions. Aliasing makes
the old path the SAME module object, so attribute access AND monkeypatching are
identical to ``reyn.mcp.registry``.
"""
from __future__ import annotations

import sys

from reyn.mcp import registry as _registry

sys.modules[__name__] = _registry
