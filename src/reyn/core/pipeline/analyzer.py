"""Pipeline static-analysis facet registry (P4 seam).

The v0.9 approach (proposal P4, "static analyzer built incrementally —
completeness-by-construction, not big-bang §7.3") mandates that no primitive
lands without its analysis contribution: ``match`` brings path enumeration,
``for_each`` / ``parallel`` bring cost + spawn-tree bounds, named-stores bring a
dataflow graph. So the analyzer is a per-step-type FACET registry, not a
monolithic walker — each primitive registers the check it is responsible for,
and the registry is the single place a caller (a future ``analyze_pipeline``
walker, or the IS-4 inline static-analysis gate) folds them together.

This module is the SEAM plus its first three entries (``call``'s, ``match``'s,
and ``fold``'s facets). It is deliberately minimal in this slice:
:data:`ANALYZER_FACETS` maps
a step dataclass type to a facet function returning a list of human-readable
problem strings ( empty = the step passed its facet), and :func:`analyze_step`
looks one up. There is no full pipeline walker wired to consume it yet — that
arrives with the primitives whose facets have real teeth (cost / spawn-tree
bounds). What matters now is that the registration point EXISTS and
``call``/``match``/``fold`` are registered, so every future primitive MUST add
its facet here rather than bolting analysis on later.

``call``'s facet is intentionally thin: the target is a STATIC literal pipeline
name (Hard rule 2), so the only thing to confirm structurally is that it IS a
non-empty literal — the parser already enforces this, so the facet is mostly the
proof that the seam is wired. The deeper ``call`` checks the full analyzer will
want (the callee is registered; the transitive spawn-tree/cost bound folds the
callee's envelope into the caller's — S5) need cross-pipeline context the
per-step facet does not have, and land with the analyzer walker + registry
plumbing, not here.

``match``'s facet has the real path-enumeration teeth this module's docstring
promises above: it walks every case LABEL (plus ``default``) and confirms each
target is a non-empty STATIC literal pipeline name (Hard rule 2 — the runtime
``on`` VALUE only ever selects a LABEL). That enumeration is the seed a future
cross-pipeline analyzer walker needs to fold every reachable case's transitive
cost/spawn-tree bound into the caller's own — same "not here yet, but the
registration point exists" posture as ``call``'s deeper checks.

``fold``'s facet is the cost-bound half of §7.3's rule 4 ("``fold`` は
``max_items``（over 使用時。省略時はランタイムデフォルト）× budget で上界を取る"):
an ``items``-sourced fold (or one with an explicit ``max_items``) has a
STATICALLY known iteration count, so the transitive cost bound (folded in by
the future walker) is a real number; an ``over``-sourced fold with no
``max_items`` reads its iteration count from a runtime ctx value the analyzer
cannot see ahead of time — flagged as a WARNING (not a hard failure — the
runtime default budget still keeps it finite, per Hard rule 9), the same
"navigating unvalidated structure" caution N5-style facets raise elsewhere."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from reyn.core.pipeline.executor import CallStep, FoldStep, MatchStep

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


def _match_facet(step: "MatchStep") -> "list[str]":
    """``match``'s real P4 contribution (more than ``call``'s placeholder):
    enumerate every (LABEL -> pipeline) case target plus ``default``, and
    confirm each is a non-empty STATIC literal pipeline name (Hard rule 2 —
    the runtime ``on`` VALUE only ever selects a LABEL, never a target).
    Also flags an empty ``cases`` mapping (the parser already rejects this,
    but the facet stays defensive for a hand-built ``MatchStep``, e.g. one
    round-tripped through ``serde``). Returns problem strings (empty = OK)."""
    problems: "list[str]" = []
    if not step.cases:
        problems.append("match step has no cases — at least one LABEL -> pipeline is required")
    targets = dict(step.cases)
    if step.default is not None:
        targets["default"] = step.default
    for label, case in targets.items():
        if not isinstance(case.pipeline, str) or not case.pipeline:
            problems.append(
                f"match case {label!r} target must be a non-empty literal "
                f"pipeline name, got {case.pipeline!r}"
            )
    return problems


def _fold_facet(step: "FoldStep") -> "list[str]":
    """Flag a ``fold`` whose list source has no STATICALLY known bound: an
    ``over`` (runtime ctx path) source with no ``max_items`` cap. An
    ``items``-sourced fold (a static literal list) always has a known length
    regardless of ``max_items``, so it never triggers this. Returns problem
    strings (empty = OK; this is a WARNING, not a hard failure — Hard rule 9's
    runtime-default budget still bounds an uncapped ``over`` fold, it is just
    not a number the analyzer can state ahead of time)."""
    problems: "list[str]" = []
    if step.over is not None and step.max_items is None:
        problems.append(
            "fold step reads its list from 'over' with no 'max_items' bound — "
            "the iteration count is not statically known ahead of run time "
            "(cost bound is runtime-list-length x budget, not a static "
            "number, until 'max_items' is set)"
        )
    return problems


# Facet registry: step type -> its static-analysis check. A future primitive
# ADDS an entry here (mirroring the executor's ``STEP_DISPATCH``, serde's
# ``ENCODERS``/``DECODERS``, and the parser's ``_STEP_PARSERS``) — the P4
# "no primitive lands without its analysis contribution" contract, made
# structural.
ANALYZER_FACETS: "dict[type, Callable[[Step], list[str]]]" = {
    CallStep: _call_facet,
    MatchStep: _match_facet,
    FoldStep: _fold_facet,
}


def analyze_step(step: "Step") -> "list[str]":
    """Run the registered static-analysis facet for `step`'s type, or return an
    empty list when the type has no facet (a step kind whose only checks are the
    parser's shape validation contributes nothing extra here)."""
    facet = ANALYZER_FACETS.get(type(step))
    return facet(step) if facet is not None else []


__all__ = ["ANALYZER_FACETS", "analyze_step"]
