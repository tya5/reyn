"""Tier 1: #5624 enforces the wire-role allow-list."""
from __future__ import annotations

import pytest

from reyn.services.compaction.engine import wire_role


def test_wire_role_accepts_known_and_internal_assistant_roles() -> None:
    """Tier 1: known wire roles and internal assistant aliases are accepted."""
    assert [wire_role(role) for role in ("user", "assistant", "tool", "system")] == [
        "user", "assistant", "tool", "system"
    ]
    assert wire_role("agent") == "assistant"
    assert wire_role("summary") == "assistant"


def test_wire_role_rejects_unknown_role_with_role_name() -> None:
    """Tier 1: an unknown role is rejected with its role name in the error."""
    with pytest.raises(ValueError, match="spill_record"):
        wire_role("spill_record")
