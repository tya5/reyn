"""Tier 2: running_plan flat_item + bundle carry plan_id_full (H-F6).

Wave-10 follow-up Topic H finding F6 (P3): the ``running_plan``
flat_item carried only ``plan_id`` (= 8-char display prefix),
omitting ``plan_id_full`` (= the canonical UUID). The
``_build_running_plan_bundle`` consumer (``c`` = copy-bundle
key on agents tab) emitted the truncated prefix, so the copied
payload disagreed with the full UUID in the events log and
broke cross-reference (= "the events tab says abc123-…- but my
copied bundle says abc123, did I get the right one?").

The ``recent_plan`` flat_item already carried ``plan_id_full``
via ``**p`` splat; this aligns the ``running_plan`` shape and
the bundle reader to prefer the full form.

Public surfaces tested:
  - running_plan flat_item includes plan_id_full
  - bundle output for a running plan starts with the full UUID
    (= matches events log identifier)
  - missing plan_id_full falls back to plan_id (regression
    guard for older callers)
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _StubRegistry:
    """Minimal registry stand-in returning one agent with one running plan."""

    def __init__(self, *, plan_id_full: str) -> None:
        self._plan_id_full = plan_id_full
        self._agents: dict = {}
        self.attached_name = "test-agent"

    def list_names(self) -> list[str]:
        return ["test-agent"]

    def loaded_names(self):  # type: ignore[no-untyped-def]
        return {"test-agent"}

    def last_activity_at(self, name: str):  # type: ignore[no-untyped-def]
        return None

    def message_count(self, name: str) -> int:
        return 0

    def recent_user_message(self, name: str) -> str:
        return ""


def _running_plan_flat_item(plan_id_full: str) -> dict:
    """Drive render_agents with a stub registry + monkeypatched plan source.

    Returns the running_plan flat_item dict so the test can inspect it.
    """
    from reyn.tui.widgets.right_panel import agents_tab

    # Monkeypatch _plans_for_agent to return our stub plan.
    original = agents_tab._plans_for_agent
    agents_tab._plans_for_agent = lambda registry, name: [  # type: ignore[assignment]
        {
            "plan_id": plan_id_full[:8],
            "plan_id_full": plan_id_full,
            "goal": "test goal",
            "done": 1,
            "total": 5,
            "failed": 0,
            "status": "running",
        },
    ]
    try:
        _, flat_items, _ = agents_tab.render_agents(
            registry=_StubRegistry(plan_id_full=plan_id_full),
            exec_state={},
            project_root=None,
            cursor=0,
        )
    finally:
        agents_tab._plans_for_agent = original  # type: ignore[assignment]

    plan_items = [i for i in flat_items if i.get("kind") == "running_plan"]
    assert plan_items, "stub setup should produce one running_plan flat_item"
    return plan_items[0]


def test_running_plan_flat_item_carries_plan_id_full() -> None:
    """Tier 2: flat_item dict has ``plan_id_full`` key with canonical UUID."""
    full = "abc12345-def6-7890-abcd-ef1234567890"
    item = _running_plan_flat_item(full)
    assert item.get("plan_id_full") == full, (
        f"running_plan flat_item should carry plan_id_full; "
        f"got item={item!r}"
    )
    # Display prefix is still 8 chars.
    assert item["plan_id"] == full[:8]


def test_running_plan_bundle_uses_plan_id_full() -> None:
    """Tier 2: ``_build_running_plan_bundle`` output starts with full UUID."""
    from reyn.tui.widgets.right_panel import RightPanel

    full = "abc12345-def6-7890-abcd-ef1234567890"
    item = _running_plan_flat_item(full)
    panel = RightPanel.__new__(RightPanel)
    bundle = panel._build_running_plan_bundle(item)
    assert full in bundle, (
        f"bundle should carry the full UUID; got:\n{bundle!r}"
    )


def test_running_plan_bundle_falls_back_on_missing_full_id() -> None:
    """Tier 2b: older callers without plan_id_full still work (regression).

    A flat_item lacking ``plan_id_full`` (= constructed by an older
    code path) should still produce a bundle using the truncated
    ``plan_id`` so the contract isn't worsened by the new key.
    """
    from reyn.tui.widgets.right_panel import RightPanel

    legacy_item = {
        "kind": "running_plan",
        "agent": "test-agent",
        "plan_id": "abc12345",
        # No plan_id_full intentionally.
        "goal": "test",
        "done": 1,
        "total": 5,
        "failed": 0,
        "status": "running",
    }
    panel = RightPanel.__new__(RightPanel)
    bundle = panel._build_running_plan_bundle(legacy_item)
    assert "abc12345" in bundle
