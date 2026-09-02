"""Tier 2: #4206 — every real ``ReynConfig`` leaf carries axis metadata
(``metadata={"axis": Axis.X}``), architect's full classification
(2026-09-02T04:01, 159-leaf table + 15 leaves resolved by direct
sibling-analogy while migrating — see this PR's own body for the
discrepancy and how each was resolved).

Gate shape (architect's own prescription): walk ``ReynConfig`` via
``walk_config_schema()`` — the SAME canonical enumeration ``reyn config
fields``/``get``/``set`` already use, never a hand-maintained list — and
assert every leaf's ``.axis`` is set. **Vacuity guard required**: an
empty leaf population would pass this check for the wrong reason (a
schema-walk regression silently emptying the population reads
identically to "every leaf classified"), so leaf non-emptiness is
asserted FIRST, separately.
"""
from __future__ import annotations

import dataclasses

import pytest

from reyn.config.config_schema import SchemaNode, walk_config_schema
from reyn.config_axis import Axis


def test_the_leaf_population_itself_is_not_empty() -> None:
    """Tier 1: vacuity guard — must run (and pass) BEFORE the coverage
    assertion below has any meaning. An empty leaf list would make
    "every leaf has an axis" vacuously true for the wrong reason (a
    schema-walk regression, not real classification coverage)."""
    leaves = walk_config_schema()
    assert leaves, "walk_config_schema() returned no leaves — the vacuity guard itself failed"


def test_every_real_leaf_has_an_axis_classification() -> None:
    """Tier 2: the gate itself — 0 leaves without ``axis`` metadata. A new
    leaf added to any ``ReynConfig`` dataclass without ``field(metadata=
    {"axis": Axis.X})`` fails this test, not silently defaulting to
    "unclassified" (architect's own explicit requirement)."""
    leaves = walk_config_schema()
    unclassified = [n.key for n in leaves if n.axis is None]
    assert not unclassified, (
        f"{len(unclassified)} leaf(ves) carry no axis metadata — every real "
        f"ReynConfig leaf must declare field(metadata={{'axis': Axis.X}}): "
        f"{sorted(unclassified)}"
    )


def test_every_axis_value_is_a_real_axis_member() -> None:
    """Tier 2: accept-side sibling — every non-``None`` axis is actually
    one of the 4 declared :class:`Axis` members, not e.g. a typo'd bare
    string that would satisfy "is not None" while being meaningless."""
    leaves = walk_config_schema()
    for node in leaves:
        assert node.axis in set(Axis), f"{node.key}: axis={node.axis!r} is not a real Axis member"


def test_strip_falsify_a_leaf_with_no_axis_metadata_fails_the_gate() -> None:
    """Tier 2: strip-falsify the gate itself — a synthetic dataclass with
    ONE field carrying no axis metadata must be caught by the SAME
    coverage check above, proving it actually bites rather than passing
    for an unrelated reason (e.g. a broken ``.axis`` attribute always
    reading ``None`` would make BOTH the real gate and this negative
    control pass, hiding the real gate never doing anything)."""
    @dataclasses.dataclass
    class _NoAxisLeaf:
        unclassified_field: int = 0

    leaves = walk_config_schema(cls=_NoAxisLeaf)
    assert leaves, "the synthetic fixture itself produced no leaves — broken test setup"
    unclassified = [n.key for n in leaves if n.axis is None]
    assert unclassified == ["unclassified_field"]


def test_strip_falsify_a_leaf_with_axis_metadata_passes() -> None:
    """Tier 2: accept-side sibling to the strip-falsify above — the SAME
    synthetic shape, but WITH axis metadata, produces zero unclassified
    leaves. Together the two prove the gate discriminates on the actual
    presence of axis metadata, not on some other property of the field."""
    @dataclasses.dataclass
    class _WithAxisLeaf:
        classified_field: int = dataclasses.field(
            default=0, metadata={"axis": Axis.PROJECT},
        )

    leaves = walk_config_schema(cls=_WithAxisLeaf)
    assert leaves
    unclassified = [n.key for n in leaves if n.axis is None]
    assert unclassified == []


# ---------------------------------------------------------------------------
# BOUNDING_KEYS / PREFERENCE_KEYS — derived-from-metadata regression pins
# ---------------------------------------------------------------------------


def test_preference_keys_content_is_unchanged_by_the_migration_to_derivation() -> None:
    """Tier 2: #4206's own migration condition — PREFERENCE_KEYS's CONTENT
    must be byte-identical to the pre-migration hand-typed 9-key set
    (only its SOURCE moved, from a literal to a schema-metadata
    derivation); a content drift here — silently gaining or losing a
    member — would be a real regression this pin catches."""
    from reyn.runtime.preferences import PREFERENCE_KEYS

    assert PREFERENCE_KEYS == frozenset({
        "output_language",
        "chat.reasoning.display",
        "cost.per_agent_tokens.warn_ratio",
        "cost.per_agent_cost_usd.warn_ratio",
        "cost.daily_tokens.warn_ratio",
        "cost.daily_cost_usd.warn_ratio",
        "cost.monthly_tokens.warn_ratio",
        "cost.monthly_cost_usd.warn_ratio",
        "cost.rate_limit_warn_ratio",
    })


def test_bounding_keys_content_is_unchanged_by_the_migration_to_derivation() -> None:
    """Tier 2: same regression pin as above, for BOUNDING_KEYS — this one
    ALSO exercises the ``override_key`` metadata dimension (``llm.model``
    derives to bare ``"model"``, not ``"llm.model"``) found live while
    migrating this exact set; a bug in that mapping would surface here as
    ``{"llm.model"}`` instead of ``{"model"}``."""
    from reyn.runtime.bounding import BOUNDING_KEYS

    assert BOUNDING_KEYS == frozenset({"model"})


def test_preference_keys_and_bounding_keys_derive_from_the_same_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: both sets ultimately read from the SAME
    ``walk_config_schema()`` enumeration — strip-falsified: replacing
    ``walk_config_schema`` with one that returns an axis-free node for
    every leaf must empty BOTH derived sets (proving neither silently
    fell back to a hand-typed default when the schema walk changes)."""
    import reyn.config.config_schema as config_schema_module

    real_walk = config_schema_module.walk_config_schema

    def _stripped_walk(cls: object = None) -> "list[SchemaNode]":
        return [
            dataclasses.replace(n, axis=None, override_enabled=False, override_key=None)
            for n in real_walk(cls)  # type: ignore[arg-type]
        ]

    monkeypatch.setattr(config_schema_module, "walk_config_schema", _stripped_walk)

    # Re-import the derivation functions fresh (they read walk_config_schema
    # lazily inside their own function bodies, so calling them again after
    # the monkeypatch is enough — no module reload needed).
    from reyn.runtime.bounding import _derive_bounding_keys
    from reyn.runtime.preferences import _derive_preference_keys

    assert _derive_preference_keys() == frozenset()
    assert _derive_bounding_keys() == frozenset()
