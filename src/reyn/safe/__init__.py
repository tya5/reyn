"""Public surface for Reyn helpers callable from safe-mode python steps.

FP-0042 — `reyn.safe.*` is the documented public path for the helpers the
allowlist (`src/reyn/kernel/_python_allowlist.py`) already grants safe-mode
steps. Pre-FP-0042 the implementations lived under `reyn.interfaces.api.safe.*`,
which the allowlist does not match — closing that gap is the reason
this package exists.

Available modules:

- :mod:`reyn.safe.file` — permission-gated file I/O (`read`, `write`,
  `glob`, `exists`, `stat`, `open`). New in FP-0042. Operates only on
  paths declared in the calling skill's ``permissions.file.read_paths`` /
  ``permissions.file.write_paths``; reads outside the declared set raise
  :class:`PermissionError`.

- :mod:`reyn.safe.cache` — 24h-TTL file cache (`get` / `set`). New in
  FP-0042 Phase 3 drift-fix; thin re-export of `reyn.core.registry.cache`
  so safe-mode skills can use the existing shared cache store.
- :mod:`reyn.safe.hash` — `sha256` / `sha256_hex`.
- :mod:`reyn.safe.http` — urllib-backed HTTP (`get` / `post` / `put` /
  `delete`). New in FP-0042 Phase 3 drift-fix; no per-call permission
  gate (Issue #571 covers the future gate design).
- :mod:`reyn.safe.json` — strict / canonical JSON helpers.
- :mod:`reyn.safe.mcp` — MCP helpers (`mcp.registry.search` / `lookup`).
  New in FP-0042 Phase 2.4 — ambient (URL hardcoded, no permission gate).
- :mod:`reyn.safe.process` — process identity (`getpid`, `pid_alive`).
  Ambient (no permission gate). New in FP-0042 Phase 2.2.
- :mod:`reyn.safe.random` — explicit-seeded RNG.
- :mod:`reyn.safe.schema` — JSON Schema validation.
- :mod:`reyn.safe.text` — named-group regex + safe templating.
- :mod:`reyn.safe.time` — monotonic / wall clock helpers (ambient).

Migration note: until FP-0042 Phase 3 lands, ``reyn.interfaces.api.safe.*`` continues
to work via shim re-exports from this package. New code should import from
``reyn.safe.*``; existing code may migrate at its own pace.
"""

from . import cache, file, hash, http, json, mcp, process, random, schema, text, time

__all__ = [
    "cache", "file", "hash", "http", "json", "mcp", "process", "random",
    "schema", "text", "time",
]
