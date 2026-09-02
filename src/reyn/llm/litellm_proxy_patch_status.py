"""#5620 — the ONE source of the proxy litellm patch's status-file path
and schema, shared by two independent readers/writers that CANNOT import
each other directly:

- **The writer**: ``scripts/litellm_proxy_patch/litellm_proxy_patch.py``
  — a standalone file, deliberately importing nothing from ``reyn``
  (owner's own 8/30 ruling: "proxy はランタイムだけで良い" — the proxy's
  own venv gets no reyn install at all, python3.13/litellm 1.95.0, not
  reyn's own dependency graph). It writes this same path/schema as a
  hand-copied literal.
- **The reader**: ``reyn doctor`` (this repo, ``src/reyn``), which reads
  the same path to report the proxy patch's own last-measured state.

A path/schema literal duplicated across 2 repos-worth of code (one file
never importable by the other) drifts silently: if the standalone patch
file's own path string goes stale relative to what doctor reads, doctor
just reports "not installed or not started" forever, and nobody notices
— architect's own #5620 design point 3 names this exact hazard
("片方が動くと黙って「not installed」に化ける"). The mitigation is not a
shared import (the standalone file cannot have one) — it is a Tier 2
gate test (``tests/llm/test_5620_litellm_proxy_patch_d.py``) that reads
BOTH copies of the literal (this module's own constant, and the string
literal inside the standalone patch file's own source) and asserts they
are equal. That gate is version-independent (a string comparison, not a
call into litellm) so it is NOT skipped by the scaffold's own litellm-
1.95.0 version pin.
"""
from __future__ import annotations

from pathlib import Path

#: Where the standalone proxy patch writes its own status, and where
#: ``reyn doctor`` reads it from. Kept as a STRING (not a computed
#: ``Path.home() / ...`` expression) specifically so the gate test can
#: compare it byte-for-byte against the standalone file's own literal —
#: a computed expression on this side and a hand-copied literal on the
#: standalone side could still drift even if the RESULTING path matched
#: today, on a `Path.home()` behavior change neither side would notice
#: together.
LITELLM_PROXY_PATCH_STATUS_PATH_STR = "~/.reyn/litellm-proxy-patch-status.json"


def litellm_proxy_patch_status_path() -> Path:
    """Return the resolved, expanded path — the one place any reyn-side
    reader should call, rather than re-expanding
    :data:`LITELLM_PROXY_PATCH_STATUS_PATH_STR` itself."""
    return Path(LITELLM_PROXY_PATCH_STATUS_PATH_STR).expanduser()


#: Keys the status file's own top-level JSON object carries. Documented
#: here (not just in the standalone patch file's own writer) so a reyn-
#: side reader has one place to check "is this key still current"
#: against, without reading the standalone file's own source.
STATUS_SCHEMA_KEYS = frozenset(
    {"pid", "litellm_version", "patched", "reached", "legacy_present", "at"},
)
