"""Tier 2: OS invariant — a bare ``RouterHostAdapter.__init__`` param is bare
only because MEASUREMENT says no other param travels to the same destinations
(#3482), never because a hand-written reason says so.

The first #3482 pass shipped a registry of 58 per-param prose reasons, most
asserting "no shared-consumer partner", behind a gate that checked only that a
reason was non-empty. Six were measurably false — the declaration's EXISTENCE
was standing in as the witness for its TRUTH. So this gate derives the
predicate from ``scripts/measure_router_host_adapter_consumers.py`` (exact
consumer-set equality, already-bundled hubs not counted twice) and the module
keeps prose ONLY for the residue a measurement cannot settle:

* a bare param that acquires an exact-match partner   -> RED (bundle them)
* a bare param with no measurable consumer            -> RED unless shelved in
                                                         ``..._CONSUMER_UNMEASURED``
* a shelved / blocked claim the measurement refutes    -> RED

No param count is pinned (that would be a Tier-4 format pin): every arm
asserts a STRUCTURE, so the gate fires the moment a new param is wired, not at
the next time someone edits a number.
"""
from __future__ import annotations

import functools
import importlib.util
import sys
from pathlib import Path

from reyn.runtime.services.router_host_adapter import (
    ROUTER_HOST_ADAPTER_BUNDLE_BLOCKED,
    ROUTER_HOST_ADAPTER_BUNDLE_TYPES,
    ROUTER_HOST_ADAPTER_CONSUMER_UNMEASURED,
    RouterHostAdapter,
)

# Phrases that assert a DERIVED predicate. The gate computes this predicate, so
# a registry reason repeating it is either redundant or (the #3482 defect) false
# — either way it is checked against the measurement, never trusted.
_NO_PARTNER_CLAIM_PHRASES = (
    "no shared-consumer partner",
    "no bundle partner",
    "no same-consumer partner",
    "no partner",
    "no cluster partner",
)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("repo root not found from " + str(here))


def _load_measurement_module():
    """Import scripts/measure_router_host_adapter_consumers.py.

    scripts/ has no ``__init__.py`` — same loader idiom as
    tests/test_check_pr_closing_intent.py uses for its sibling script.
    """
    path = _repo_root() / "scripts" / "measure_router_host_adapter_consumers.py"
    assert path.is_file(), f"measurement script missing: {path}"
    spec = importlib.util.spec_from_file_location("_rha_consumer_measure", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@functools.lru_cache(maxsize=1)
def _measure():
    """Measure once per session — every arm reads the same snapshot, and the
    scan parses all of src/reyn (seconds, not milliseconds)."""
    return _load_measurement_module().measure(_repo_root())


def test_measurement_is_not_vacuous() -> None:
    """Tier 2: vacuity guard — every other arm here is only as strong as the
    measurement it derives from, and a silently empty measurement (parser
    drifted off the signature, external scan matched nothing) would make all
    of them pass by finding nothing to complain about."""
    m = _measure()

    assert m.params, "AST found zero __init__ params — the parser drifted off the signature."
    assert m.bundle_types, (
        "ROUTER_HOST_ADAPTER_BUNDLE_TYPES is empty — no annotation could be "
        "recognized as a bundle, so every param would look bare."
    )
    assert [p for p in m.params if p.is_bundled], (
        "no param resolved to a bundle type — the annotation/bundle-name match broke."
    )
    assert m.bundled_consumers, (
        "no already-bundled consumer was derived — the hub-exclusion rule went "
        "dead, which silently re-admits the equality a landed bundle already absorbed."
    )
    assert [p for p in m.params if p.external_sites], (
        "the external host-surface scan found no reader anywhere under src/reyn — "
        "with it dead every param looks 'consumer unmeasured' and the shelf "
        "swallows the whole signature."
    )
    assert [p for p in m.params if p.adapter_members], (
        "no param resolved to an adapter member — the in-class reader scan went dead, "
        "which erases the destination level that distinguishes append_history from put_outbox."
    )


def test_no_bare_param_has_an_unbundled_exact_match_partner() -> None:
    """Tier 2: the derived N+1 arm. Two params carried to exactly the same set
    of destinations are one bundle; if they are still bare, this fails and
    names the pair. A param wired tomorrow into an existing lane's consumer
    goes RED at the moment it is wired — no registry edit can silence it."""
    m = _measure()
    offenders = {
        cluster: sorted(m.by_name()[cluster[0]].consumers)
        for cluster in m.exact_match_clusters()
        if not all(name in ROUTER_HOST_ADAPTER_BUNDLE_BLOCKED for name in cluster)
    }
    assert not offenders, (
        "These bare RouterHostAdapter.__init__ params share an EXACT consumer set "
        "and must be one bundle (a frozen dataclass, no defaults, no construction "
        "logic) — or, if bundling is impossible for a reason no measurement can "
        "produce, be registered in ROUTER_HOST_ADAPTER_BUNDLE_BLOCKED:\n"
        + "\n".join(f"  {list(c)}\n      consumers: {cons}" for c, cons in offenders.items())
    )


def test_params_with_no_measurable_consumer_are_shelved_with_a_reason() -> None:
    """Tier 2: "not measurable" is a different shelf from "has no partner", and
    the gate keeps them from merging — in both directions, so a param that
    gains a consumer must leave the shelf and one that loses its last consumer
    must join it. The reason is the part a scan cannot supply."""
    m = _measure()
    measured_unmeasurable = set(m.unmeasured_params())
    shelved = set(ROUTER_HOST_ADAPTER_CONSUMER_UNMEASURED)

    unshelved = measured_unmeasurable - shelved
    assert not unshelved, (
        "These bare params have NO measurable consumer and no shelf entry: "
        f"{sorted(unshelved)}. Add each to ROUTER_HOST_ADAPTER_CONSUMER_UNMEASURED "
        "with what IS known (dynamic read / external surface / dead wiring under "
        "review) — do not claim 'no partner', which is a different and stronger claim."
    )
    stale = shelved - measured_unmeasurable
    assert not stale, (
        "These params are shelved as having no measurable consumer, but the "
        f"measurement finds one: {sorted(stale)}. The shelf entry is now false — "
        "delete it (and bundle the param if it acquired an exact-match partner)."
    )
    for name, reason in ROUTER_HOST_ADAPTER_CONSUMER_UNMEASURED.items():
        assert reason and reason.strip(), (
            f"shelf entry {name!r} has an empty reason — an entry with no reason "
            "is a bare param wearing a disguise, the exact #3482 defect."
        )


def test_written_claims_agree_with_the_measurement() -> None:
    """Tier 2: a claim contradicted by measurement goes RED (#3482 firm ④B).

    Two shapes: a ``BUNDLE_BLOCKED`` entry for a param the measurement gives no
    partner (the exception marker never fires — it is stale or was never true),
    and any reason text asserting the derived "no partner" predicate about a
    param that measurably HAS a partner."""
    m = _measure()
    by_name = m.by_name()

    unknown = set(ROUTER_HOST_ADAPTER_BUNDLE_BLOCKED) | set(ROUTER_HOST_ADAPTER_CONSUMER_UNMEASURED)
    unknown -= set(by_name)
    assert not unknown, (
        f"registry entries name params that do not exist in __init__: {sorted(unknown)}"
    )

    dead_markers = {
        name: reason
        for name, reason in ROUTER_HOST_ADAPTER_BUNDLE_BLOCKED.items()
        if not m.partners_of(name)
    }
    assert not dead_markers, (
        "ROUTER_HOST_ADAPTER_BUNDLE_BLOCKED claims these params have an "
        "exact-match partner that cannot be bundled, but the measurement finds "
        f"no partner for them: {sorted(dead_markers)}. The claim is false (or has "
        "gone stale) — delete the entry rather than leave a marker that never fires."
    )

    contradicted = {}
    for registry in (ROUTER_HOST_ADAPTER_CONSUMER_UNMEASURED, ROUTER_HOST_ADAPTER_BUNDLE_BLOCKED):
        for name, reason in registry.items():
            lowered = reason.lower()
            if not any(phrase in lowered for phrase in _NO_PARTNER_CLAIM_PHRASES):
                continue
            partners = m.partners_of(name)
            if partners:
                contradicted[name] = partners
    assert not contradicted, (
        "These registry reasons assert that the param has no shared-consumer "
        "partner, and the measurement exhibits one — the #3482 defect exactly: "
        + "; ".join(f"{n} shares its consumer set with {list(p)}" for n, p in contradicted.items())
        + ". The predicate is derived by this gate; do not restate it in prose."
    )


def test_every_bundle_type_is_a_real_default_free_dataclass() -> None:
    """Tier 2: the bundle registry names real frozen dataclasses with NO field
    defaults. A default would let a caller's silent omission absorb a wiring
    change, which is what the byte-identical-refactor invariant forbids."""
    import dataclasses

    import reyn.runtime.services.router_host_adapter as mod

    for type_name in ROUTER_HOST_ADAPTER_BUNDLE_TYPES:
        bundle = getattr(mod, type_name, None)
        assert bundle is not None and dataclasses.is_dataclass(bundle), (
            f"{type_name} is in ROUTER_HOST_ADAPTER_BUNDLE_TYPES but is not a "
            "dataclass in router_host_adapter.py"
        )
        fields = dataclasses.fields(bundle)
        assert fields, f"{type_name} has no fields — an empty bundle carries nothing"
        defaulted = [
            f.name
            for f in fields
            if f.default is not dataclasses.MISSING
            or f.default_factory is not dataclasses.MISSING  # type: ignore[misc]
        ]
        assert not defaulted, (
            f"{type_name} fields have defaults ({defaulted}) — every field must be "
            "explicit at the call site so an omitted wire cannot pass silently."
        )


def test_router_host_adapter_is_the_real_class_under_gate() -> None:
    """Tier 2: sanity — the measured file is the module the gate imports from,
    so a green result above is about the class actually in use."""
    m = _measure()
    measured = {p.name for p in m.params}
    live = set(RouterHostAdapter.__init__.__code__.co_varnames[
        1 : RouterHostAdapter.__init__.__code__.co_argcount
        + RouterHostAdapter.__init__.__code__.co_kwonlyargcount
    ])
    assert measured == live, (
        "the AST measurement and the imported class disagree about the param set "
        f"— only-in-AST {sorted(measured - live)}, only-in-class {sorted(live - measured)}"
    )
