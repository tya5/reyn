"""reyn.data.presentations — the named-template registry for the present layer (FP-0054 PR-C).

Named presentation templates live in ``.reyn/config/presentations.yaml`` and are
registered the SAME way ``skills.entries`` / ``pipelines.entries`` are (config
section → per-entry declaration → hot-reload seam at the turn boundary).

A named template's value is a **blueprint** — the identical declarative,
non-executable component tree an inline blueprint is — validated through
``reyn.core.present.validate_blueprint`` at registry-build time (so a malformed
template is caught at config load, per-entry isolated, never at render time).
Registering a named template is an **operator/config action**: the write-gate
culture means the LLM authors inline blueprints only and never registers a named
template.
"""
from __future__ import annotations

from reyn.data.presentations.registry import (
    PresentationLoadError,
    PresentationRegistry,
    build_presentation_registry,
)

__all__ = [
    "PresentationLoadError",
    "PresentationRegistry",
    "build_presentation_registry",
]
