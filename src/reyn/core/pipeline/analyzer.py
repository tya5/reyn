"""Pipeline static-analysis facet registry (P4 seam).

The v0.9 approach (proposal P4, "static analyzer built incrementally —
completeness-by-construction, not big-bang §7.3") mandates that no primitive
lands without its analysis contribution: ``match`` brings path enumeration,
``for_each`` / ``parallel`` bring cost + spawn-tree bounds, named-stores bring a
dataflow graph. So the analyzer is a per-step-type FACET registry, not a
monolithic walker — each primitive registers the check it is responsible for,
and the registry is the single place a caller (a future ``analyze_pipeline``
walker, or the IS-4 inline static-analysis gate) folds them together.

This module is the SEAM plus the first entry (``call``'s facet). It is
deliberately minimal in this slice: :data:`ANALYZER_FACETS` maps a step
dataclass type to a facet function returning a list of human-readable problem
strings ( empty = the step passed its facet), and :func:`analyze_step` looks one
up. There is no full pipeline walker wired to consume it yet — that arrives with
the primitives whose facets have real teeth (cost / spawn-tree bounds). What
matters now is that the registration point EXISTS and ``call`` is registered, so
every future primitive MUST add its facet here rather than bolting analysis on
later.

``call``'s facet is intentionally thin: the target is a STATIC literal pipeline
name (Hard rule 2), so the only thing to confirm structurally is that it IS a
non-empty literal — the parser already enforces this, so the facet is mostly the
proof that the seam is wired. The deeper ``call`` checks the full analyzer will
want (the callee is registered; the transitive spawn-tree/cost bound folds the
callee's envelope into the caller's — S5) need cross-pipeline context the
per-step facet does not have, and land with the analyzer walker + registry
plumbing, not here.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from reyn.core.pipeline.executor import CallStep

if TYPE_CHECKING:
    from reyn.core.pipeline.executor import Step


def _call_facet(step: "CallStep") -> "list[str]":
    """Confirm a ``call`` targets a non-empty STATIC literal pipeline name
    (Hard rule 2). Returns problem strings (empty = OK)."""
    problems: "list[str]" = []
    if not isinstance(step.pipeline, str) or not step.pipeline:
        problems.append(
            f"call step target must be a non-empty literal pipeline name, "
            f"got {step.pipeline!r}"
        )
    return problems


# Facet registry: step type -> its static-analysis check. A future primitive
# ADDS an entry here (mirroring the executor's ``STEP_DISPATCH``, serde's
# ``ENCODERS``/``DECODERS``, and the parser's ``_STEP_PARSERS``) — the P4
# "no primitive lands without its analysis contribution" contract, made
# structural.
ANALYZER_FACETS: "dict[type, Callable[[Step], list[str]]]" = {
    CallStep: _call_facet,
}


def analyze_step(step: "Step") -> "list[str]":
    """Run the registered static-analysis facet for `step`'s type, or return an
    empty list when the type has no facet (a step kind whose only checks are the
    parser's shape validation contributes nothing extra here)."""
    facet = ANALYZER_FACETS.get(type(step))
    return facet(step) if facet is not None else []


__all__ = ["ANALYZER_FACETS", "analyze_step"]
