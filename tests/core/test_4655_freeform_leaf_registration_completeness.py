"""Tier 1: every ``is_dict_leaf`` field in the ``ReynConfig`` schema has an
EXPLICIT #4655 registration disposition — never silence.

Same shape as #1983 (``Op`` union ↔ ``OP_KIND_MODEL_MAP``) and #4646
(parser step-kinds ↔ ``executor.STEP_KINDS``): a derived pair, checked for
drift, instead of a hand-maintained list nobody re-verifies. The LEFT side
is ``walk_config_schema()``'s live ``is_dict_leaf`` set (the same schema
walk ``reyn config fields`` shows); the RIGHT side is the union of the two
explicit registries a free-form leaf can be filed under —
``register_freeform_leaf_validator`` (Kind① — a real inner-vocabulary
check) and ``register_freeform_leaf_open`` (Kind② — "we looked, it's
genuinely open"). #4655's own defect was 17 dict-leaf fields sitting in
neither registry, indistinguishable from a leaf nobody had ever looked at
— exactly the state a *silent* "don't register anything for this key"
convention cannot be told apart from. This module's job is closing that:
a THIRD dict-leaf field added later without an explicit disposition must
make this test go RED, not silently join the same unregistered pile.

Every module that calls either registration function at import time must
be imported before :func:`unregistered_freeform_leaves` is read, or its
leaves report as unregistered false-positively — ``reyn.config.loader``
imports every config submodule (``chat`` / ``embedding`` / ``execution`` /
``infra`` / ``media`` / ``observability`` / ``root``) that currently owns a
registration, so importing it here is both necessary and sufficient today.
A future registration module living somewhere ``loader`` does not import
would need its own explicit import here — the same trap this module's own
docstring warns every reader about.
"""
from __future__ import annotations

from reyn.config import config_schema, loader  # noqa: F401

# `loader`'s import (above) is the completeness precondition: it
# transitively imports every config submodule (chat/embedding/execution/
# infra/media/observability/root) that currently calls
# register_freeform_leaf_validator/register_freeform_leaf_open at
# module-import time. Removing this import would make every one of those
# registrations silently never run, and this test would false-positive RED
# for every dict-leaf field rather than exercising the real completeness
# invariant.


def test_every_dict_leaf_has_an_explicit_4655_registration() -> None:
    """Tier 1: ``unregistered_freeform_leaves()`` is empty — every
    ``is_dict_leaf`` key from the live schema walk is registered as either
    Kind① or Kind②. A RED here means a dict-leaf field was added (or given
    ``is_dict_leaf=True`` via the ``dict_leaf`` metadata escape hatch)
    without anyone deciding what its inner vocabulary should be — go
    register it, do not weaken this assertion."""
    assert config_schema.unregistered_freeform_leaves() == frozenset()


def test_a_real_dict_leaf_is_registered_as_kind_one_or_kind_two() -> None:
    """Tier 1: accept-side sanity — a real, live dict-leaf key
    (``sandbox.policy``, the pre-#4655 precedent) is unambiguously
    ``"validated"`` (Kind①), confirming the completeness check's own
    inputs are wired to the real registries and not two empty stand-ins
    that would trivially satisfy the frozenset-difference-is-empty
    assertion above for the wrong reason."""
    assert config_schema.freeform_leaf_registration_kind("sandbox.policy") == "validated"


def test_completeness_check_goes_red_for_a_genuinely_unregistered_leaf() -> None:
    """Tier 1: strip-falsify — proves ``unregistered_freeform_leaves()`` can
    actually report a non-empty result, not just happen to always compute
    to the empty set regardless of what the registries contain. Directly
    manipulates the two registries (there is no public "unregister" API —
    registration is meant to be a one-way, load-time declaration; the
    registries themselves ARE the mechanism under test, not incidental
    internal state a public accessor could stand in for) within the
    test's own scope, restored via try/finally so this test cannot leak
    state into any other test in the same process, to simulate a dict-leaf
    key that is a live schema member but was never registered under
    either kind — the exact #4655 defect this whole mechanism exists to
    catch."""
    _namespaces, dict_leaves, _scalars = config_schema._schema_index()
    assert dict_leaves, "no dict-leaf fields in the live schema — test premise broken"
    a_real_dict_leaf_key = next(iter(dict_leaves))

    saved_validators = dict(config_schema._FREEFORM_LEAF_VALIDATORS)
    saved_open = set(config_schema._FREEFORM_LEAF_DECLARED_OPEN)
    try:
        config_schema._FREEFORM_LEAF_VALIDATORS.pop(a_real_dict_leaf_key, None)
        config_schema._FREEFORM_LEAF_DECLARED_OPEN.discard(a_real_dict_leaf_key)
        assert a_real_dict_leaf_key in config_schema.unregistered_freeform_leaves()
    finally:
        config_schema._FREEFORM_LEAF_VALIDATORS.clear()
        config_schema._FREEFORM_LEAF_VALIDATORS.update(saved_validators)
        config_schema._FREEFORM_LEAF_DECLARED_OPEN.clear()
        config_schema._FREEFORM_LEAF_DECLARED_OPEN.update(saved_open)

    # Restoration itself must actually restore the green state — a broken
    # finally-block would leave every OTHER test in the process false-red.
    assert config_schema.unregistered_freeform_leaves() == frozenset()


def test_a_key_registered_open_is_not_also_required_to_have_a_validator() -> None:
    """Tier 1: accept-side — Kind② registration alone (no Kind① validator)
    is sufficient to clear a leaf from ``unregistered_freeform_leaves()``,
    confirming the completeness check treats the two registries as a
    UNION, not requiring both, matching #4655's own "exactly one of the
    two kinds" design (never both, never neither)."""
    assert config_schema.freeform_leaf_registration_kind("permissions") == "open"
    assert "permissions" not in config_schema.unregistered_freeform_leaves()
