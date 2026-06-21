"""Tier 2: #1983 — the ControlIROp union is single-sourced from OP_KIND_MODEL_MAP.

Finding 3: ``mcp_drop_server`` was registered (universal_dispatch), documented
(``control-ir.md``), modeled (``MCPDropServerIROp``) and handled by the executor,
but ABSENT from the hand-listed ``ControlIROp`` discriminated union and from
``OP_KIND_MODEL_MAP`` — so a phase emitting it failed ``ActOutput`` validation
(advertise-but-don't-enforce; the worst direction of the control-ir.md ↔ map
sync invariant).

The fix relocates the map to ``schemas/models.py`` and DERIVES the union from it
(``Union[(FileIROp, *OP_KIND_MODEL_MAP.values())]``) so completeness is by
construction. These are the regression guards on that construction: a map kind
that fails to surface in the union — or a hand-added union member outside the
map — breaks here. Real models + the real linter; no mocks.

``OP_KIND_MODEL_MAP`` / ``ALL_OP_KINDS`` are imported LOCALLY (inside the
construction tests) so this module collects against the PRE-fix tree too,
letting the falsifying tests give a clean RED when the fix is reverted.
"""
from __future__ import annotations

from typing import get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from reyn.schemas.models import ActOutput, ControlIROp


def _union_member_kinds() -> set[str]:
    """The literal ``kind`` of every member of the ControlIROp discriminated union.

    ``ControlIROp == Annotated[Union[...], Field(discriminator="kind")]`` →
    ``get_args(...)[0]`` is the ``Union``; its args are the member models; each
    member's ``kind`` field is a ``Literal["<kind>"]``.
    """
    union = get_args(ControlIROp)[0]
    kinds: set[str] = set()
    for member in get_args(union):
        kind_ann = member.model_fields["kind"].annotation
        kinds.add(get_args(kind_ann)[0])
    return kinds


def test_union_members_complete_vs_map():
    """Tier 2: union member kinds == OP_KIND_MODEL_MAP keys ∪ {"file"}.

    Falsify: a kind in the map but missing from the union (the Finding-3 bug) —
    or a hand-added union member outside the map — breaks this. The coarse
    ``file`` op (``FileIROp``) is the only deliberate non-map member."""
    from reyn.schemas.models import OP_KIND_MODEL_MAP

    expected = set(OP_KIND_MODEL_MAP) | {"file"}
    actual = _union_member_kinds()
    missing_from_union = expected - actual
    extra_in_union = actual - expected
    assert not missing_from_union, f"map kinds missing from union: {sorted(missing_from_union)}"
    assert not extra_in_union, f"union members outside map (+file): {sorted(extra_in_union)}"


def test_all_op_kinds_is_exactly_map_keys():
    """Tier 2: ALL_OP_KINDS is exactly the map keys (single-source frozenset —
    no separately-maintained kind list can drift from the map)."""
    from reyn.schemas.models import ALL_OP_KINDS, OP_KIND_MODEL_MAP

    assert set(ALL_OP_KINDS) == set(OP_KIND_MODEL_MAP)


def test_mcp_drop_server_validates_through_act_output():
    """Tier 2: Finding-3 falsifying — an act turn carrying an ``mcp_drop_server``
    op validates. PRE-fix (kind absent from the union) this raised
    ValidationError — the exact bug: registered + documented + handled but not
    enforceable. Revert the fix and this goes RED."""
    out = ActOutput.model_validate(
        {"type": "act", "ops": [{"kind": "mcp_drop_server", "server": "stale_srv"}]}
    )
    assert out.ops[0].kind == "mcp_drop_server"
    assert out.ops[0].server == "stale_srv"


def test_mcp_drop_server_is_a_known_allowed_op():
    """Tier 2: Finding-3 falsifying — ``mcp_drop_server`` is in ``ALL_TOOL_NAMES``,
    the complete op-kind set the DSL linter validates ``allowed_ops`` against
    (``linter.py`` imports it as ``_KNOWN_ALLOWED_OPS_NAMES``). PRE-fix the kind
    was absent from the set, so a phase declaring it in ``allowed_ops`` was
    flagged 'not a known Control IR op kind' and silently filtered at runtime —
    advertised + handled but not declarable."""
    from reyn.core.op_runtime.registry import ALL_TOOL_NAMES

    assert "mcp_drop_server" in ALL_TOOL_NAMES


def test_derived_union_still_rejects_unknown_kind():
    """Tier 2: deriving the union from the map did NOT open the discriminator to
    arbitrary kinds — an unknown kind is still rejected (closed-set invariant)."""
    adapter = TypeAdapter(ControlIROp)
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "no_such_op_kind_xyz", "server": "x"})
