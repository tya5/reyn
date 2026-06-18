"""Public surface for Reyn helpers callable from safe-mode python steps.

``reyn.api.safe.*`` is the public import path for the safe-mode helpers. The
``_python_allowlist`` (`src/reyn/core/kernel/_python_allowlist.py`) grants
safe-mode python steps an **allow-of-one** over this prefix — only
``reyn.api.safe.*`` is importable from a ``mode: safe`` step; everything else is
default-deny. Each module here is vetted to expose only operations that are safe
to call from an untrusted step (the real boundary is the vetted allow-of-one,
not import-blocking).

Available modules:

- :mod:`reyn.api.safe.file` — permission-gated file I/O (`read`, `write`,
  `glob`, `exists`, `stat`, `open`). New in FP-0042. Operates only on
  paths declared in the calling skill's ``permissions.file.read_paths`` /
  ``permissions.file.write_paths``; reads outside the declared set raise
  :class:`PermissionError`.

- :mod:`reyn.api.safe.cache` — 24h-TTL file cache (`get` / `set`). New in
  FP-0042 Phase 3 drift-fix; thin re-export of `reyn.core.registry.cache`
  so safe-mode skills can use the existing shared cache store.
- :mod:`reyn.api.safe.hash` — `sha256` / `sha256_hex`.
- :mod:`reyn.api.safe.http` — urllib-backed HTTP (`get` / `post` / `put` /
  `delete`). New in FP-0042 Phase 3 drift-fix; no per-call permission
  gate (Issue #571 covers the future gate design).
- :mod:`reyn.api.safe.json` — strict / canonical JSON helpers.
- :mod:`reyn.api.safe.mcp` — MCP helpers (`mcp.registry.search` / `lookup`).
  New in FP-0042 Phase 2.4 — ambient (URL hardcoded, no permission gate).
- :mod:`reyn.api.safe.process` — process identity (`getpid`, `pid_alive`).
  Ambient (no permission gate). New in FP-0042 Phase 2.2.
- :mod:`reyn.api.safe.random` — explicit-seeded RNG.
- :mod:`reyn.api.safe.schema` — JSON Schema validation.
- :mod:`reyn.api.safe.text` — named-group regex + safe templating.
- :mod:`reyn.api.safe.time` — monotonic / wall clock helpers (ambient).
"""

from . import cache, file, hash, http, json, mcp, process, random, schema, text, time

__all__ = [
    "cache", "file", "hash", "http", "json", "mcp", "process", "random",
    "schema", "text", "time",
]
