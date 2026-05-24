"""Tier 2: attached agent is pinned to top of agents tab (H-F12).

Wave-10 follow-up Topic H finding F12 (P3): the agents tab
iterated ``registry.list_names()`` which returns
sorted-alphabetical. On a registry with 5+ agents, the attached
agent (= the one the user is currently chatting with) could
appear at position 3-5 and require j-key navigation to find.
Daily-contact agents should be immediately visible.

After the fix the attached agent is pinned to position 0; the
rest stays alphabetical (= secondary sort key).

Public surfaces tested:
  - attached agent appears first in flat_items
  - non-attached agents preserve alphabetical order
  - no-attached scenario falls back to pure alphabetical
    (regression guard)
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _StubRegistry:
    """Stub carrying the methods render_agents calls."""

    def __init__(self, *, names: list[str], attached: str | None) -> None:
        self._names = sorted(names)  # mirror real list_names() contract
        self._attached = attached
        self._agents: dict = {}

    @property
    def attached_name(self) -> str | None:
        return self._attached

    def list_names(self) -> list[str]:
        return list(self._names)

    def loaded_names(self):  # type: ignore[no-untyped-def]
        return set(self._names)

    def last_activity_at(self, name: str):  # type: ignore[no-untyped-def]
        return None

    def message_count(self, name: str) -> int:
        return 0

    def recent_user_message(self, name: str) -> str:
        return ""


def _agent_order(registry) -> list[str]:
    """Run render_agents and extract the agent-row order from flat_items."""
    from reyn.chat.tui.widgets.right_panel.agents_tab import render_agents

    _, flat_items, _ = render_agents(
        registry=registry, exec_state={}, project_root=None, cursor=0,
    )
    return [item["name"] for item in flat_items if item.get("kind") == "agent"]


def test_attached_agent_pinned_to_top_when_alphabetically_middle() -> None:
    """Tier 2: attached agent at position 0 even if mid-alphabetical."""
    reg = _StubRegistry(
        names=["alpha", "beta", "gamma", "delta", "epsilon"],
        attached="gamma",  # would sort to position 4 alphabetically
    )
    order = _agent_order(reg)
    assert order[0] == "gamma", (
        f"attached 'gamma' should be at position 0, got order={order}"
    )
    # Remaining stays alphabetical.
    assert order[1:] == sorted([n for n in reg.list_names() if n != "gamma"])


def test_attached_agent_pinned_when_already_first() -> None:
    """Tier 2: when attached IS alphabetically first, order is unchanged."""
    reg = _StubRegistry(
        names=["alpha", "beta", "gamma"],
        attached="alpha",
    )
    order = _agent_order(reg)
    assert order == ["alpha", "beta", "gamma"]


def test_no_attached_falls_back_to_alphabetical() -> None:
    """Tier 2b: no attached agent → pure alphabetical order (regression)."""
    reg = _StubRegistry(
        names=["zulu", "alpha", "mike"],
        attached=None,
    )
    order = _agent_order(reg)
    assert order == ["alpha", "mike", "zulu"]
