"""Present layer core — declarative, non-executable user-facing presentation (FP-0054).

The building blocks the ``present`` op composes: a display-only component
``catalog`` + structural blueprint gate, JSON-Pointer ``binding`` resolution
against a null renderer, the output-side presentation ``guard``, and the
``resolve_present_source`` seam that re-hydrates a ``data_ref`` under ``file.read``
authority. Surface renderers (inline-CUI, web, A2A) consume this model in later
PRs; this package produces the model + audit stats only.
"""
from __future__ import annotations

from reyn.core.present.binding import ResolvedPresentation, resolve_bindings, resolve_pointer
from reyn.core.present.catalog import CATALOG, PresentBlueprintError, validate_blueprint
from reyn.core.present.renderer import PresentationRenderer
from reyn.core.present.source import (
    PresentSourceNotFound,
    compute_ingested,
    resolve_present_source,
)

__all__ = [
    "CATALOG",
    "PresentBlueprintError",
    "PresentSourceNotFound",
    "PresentationRenderer",
    "ResolvedPresentation",
    "compute_ingested",
    "resolve_bindings",
    "resolve_pointer",
    "resolve_present_source",
    "validate_blueprint",
]
