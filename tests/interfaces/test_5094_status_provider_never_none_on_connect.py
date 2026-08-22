"""Tier 2: #5094, third layer — AG-UI's status provider must never return
``None`` on a fresh ``--connect``, and the Agent pane must never silently
drop an agent the tree hasn't caught up to yet.

Owner STILL live-blocked (relayed via architect/lead-coder) after #5097
(wire forwarding) and #5104 (``list_active_names()`` vs ``loaded_names()``)
both landed. Root cause, measured directly off ``ensure_running``'s own
docstring and ``status._snapshot``'s own body: ``agui/endpoint.py``'s
``_status_provider`` called ``status._snapshot(registry)`` — which reads
``registry.attached_session()``, the registry's single GLOBAL attach
pointer — and returns ``None`` wholesale the instant that pointer is unset.
``AgentRegistry.ensure_running`` (the boot primitive AG-UI's connection
handler actually calls) deliberately never sets that pointer (#3793 stage
2: AG-UI tracks attach state itself, per connection, via
``SurfaceManager``). So the FIRST snapshot of every AG-UI connection —
before any ``/attach``-request round-trip — was unconditionally ``None``:
not just the agent roster, the WHOLE status dict, regardless of what
#5097/#5104 already fixed inside it.

Fix: ``status._snapshot_for_session(registry, s, config)`` takes the
``Session`` as a parameter instead of looking one up off the registry's
global pointer; ``endpoint.py``'s ``_status_provider`` calls it with the
connection's OWN already-resolved ``session`` (bound via
``ensure_running``), so it never depends on whether some OTHER connection
happens to hold the global focus.

Companion fix (lead-coder finding, issuecomment-5379848710): once a
snapshot can carry a PARTIAL ``session_tree`` (not just wholly-empty-or-
full), ``chrome.py``'s ``_agent_pane_entries`` either/or branch
(``if tree: ... else: <flat list>``) silently dropped every agent NOT in
the tree the moment even one other agent had a tree entry. Fixed to union
the two sources — witnessed here too, in the same PR that opened the door
to a partial tree.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.inline.textual_chat.chrome import agent_pane_options
from reyn.interfaces.repl.status import _snapshot_for_session
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """A real AgentRegistry whose factory builds real Sessions on demand —
    same pattern as test_registry_session_tree.py's own fixture."""
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


@pytest.mark.asyncio
async def test_snapshot_for_session_is_a_real_dict_with_nothing_globally_attached(
    tmp_path: Path,
) -> None:
    """Tier 2: the exact #5094 scenario — ``ensure_running`` never sets the
    registry's global attach pointer (mirroring AG-UI's real connect path),
    yet a caller holding the concrete ``Session`` still gets a real
    snapshot, not the ``None`` a global-pointer read would give."""
    reg = _make_registry(tmp_path)
    session = await reg.ensure_running("default")

    # The precondition the bug depended on: nothing globally attached.
    assert reg.attached_session() is None
    assert reg.attached_name is None

    snap = _snapshot_for_session(reg, session)
    assert snap is not None, "a caller with a concrete Session must never see None"
    assert snap["attached_name"] == "default"
    assert set(snap["agent_names"]) == {"default", "alpha"}
    assert {e["agent"] for e in snap["session_tree"]} == {"default", "alpha"}


@pytest.mark.asyncio
async def test_strip_falsifier_the_global_snapshot_still_returns_none_unattached(
    tmp_path: Path,
) -> None:
    """Tier 2: regression guard on the OTHER half of the split — ``_snapshot``
    (the global-pointer path LOCAL still uses) is UNCHANGED: still ``None``
    when nothing is globally attached. Proves the fix added a new path for
    AG-UI rather than silently changing LOCAL's own contract (which a
    dedicated test double, ``_NoneReadModel`` in
    test_textual_chat_attach_state_3671_p3.py, depends on to simulate the
    pre-attach boot window)."""
    from reyn.interfaces.repl.status import _snapshot

    reg = _make_registry(tmp_path)
    await reg.ensure_running("default")  # same boot path, still no global attach

    assert _snapshot(reg) is None


def test_agent_pane_unions_the_tree_with_the_flat_roster_not_either_or() -> None:
    """Tier 1: lead-coder's finding — a PARTIAL ``session_tree`` (1 of 4
    agents) must not make the other 3 vanish. Measured in-process by
    architect: agent_names=4 + session_tree=1 rendered as 1 row before this
    fix; must render 4 rows after."""
    names = ["default", "neo", "coder-smith", "coder-brown"]
    tree = [{"agent": "default", "attached": True, "sessions": []}]

    rows = agent_pane_options(names, active="default", tree=tree)

    rendered = {r.split("  · active")[0].lstrip("▸ ").strip() for r in rows}
    assert rendered == set(names), (
        f"partial tree dropped agents: expected {set(names)!r}, got {rendered!r}"
    )
    for missing in ("neo", "coder-smith", "coder-brown"):
        assert missing in rendered, f"{missing!r} (not in the tree) was dropped: {rows!r}"


def test_agent_pane_never_double_renders_an_agent_present_in_both() -> None:
    """Tier 1: regression guard — an agent WITH a tree entry must not also
    get a flat-list row (union, not concatenation)."""
    names = ["default", "alpha"]
    tree = [
        {"agent": "default", "attached": True, "sessions": [{"sid": "main", "attached": True}]},
        {"agent": "alpha", "attached": False, "sessions": []},
    ]
    rows = agent_pane_options(names, active="default", tree=tree)
    # An agent-level row is one with no leading session indent (4 spaces) —
    # every agent name must own exactly ONE such row, never two (a tree row
    # AND a flat row for the same agent).
    agent_rows = [r for r in rows if not r.startswith("    ")]
    for name in names:
        matches = [r for r in agent_rows if name in r]
        assert matches, f"{name!r} has no agent-level row at all: {rows!r}"
        assert matches[1:] == [], (
            f"{name!r} rendered more than one agent-level row — union must not "
            f"concatenate a tree row with a flat row: {rows!r}"
        )
