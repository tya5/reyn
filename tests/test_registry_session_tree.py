"""Tier 2: AgentRegistry.session_tree() — read-only snapshot accessor.

Exercises ``session_tree()`` against a REAL AgentRegistry with REAL Sessions
loaded through the public sync path (``get_or_load`` / ``spawn_session`` + a real
session factory — same pattern as test_registry_multi_session_1726). State is
never set up by mutating private attrs. Covers the shape contract, multi-agent /
multi-session listing, sid sorting, the unattached (all-false) marking, and
snapshot isolation. Attached=True *rendering* (the ▸ marker) is covered by the
``_agent_expansion`` test in test_inline_pr3b_status_menu.py.
"""
from __future__ import annotations

from pathlib import Path

from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """A real AgentRegistry whose factory builds real Sessions on demand."""
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return Session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    # A second agent on disk so multi-agent listing is reachable via get_or_load.
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


def test_session_tree_empty_when_no_sessions_loaded(tmp_path: Path) -> None:
    """Tier 2: session_tree() returns [] when no sessions are loaded."""
    reg = _make_registry(tmp_path)
    assert reg.session_tree() == []


def test_session_tree_entry_shape_for_loaded_agent(tmp_path: Path) -> None:
    """Tier 2: a loaded agent's entry has 'agent', 'attached', 'sessions' keys; with
    nothing attached its flag is False and its 'main' session is listed."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("default")

    by_name = {e["agent"]: e for e in reg.session_tree()}
    assert "default" in by_name
    entry = by_name["default"]
    assert set(entry.keys()) == {"agent", "attached", "sessions"}
    assert entry["attached"] is False          # nothing attached yet
    by_sid = {s["sid"]: s for s in entry["sessions"]}
    assert "main" in by_sid


def test_session_tree_session_entry_shape(tmp_path: Path) -> None:
    """Tier 2: each session entry has exactly 'sid' and 'attached' keys."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("default")

    sess_list = reg.session_tree()[0]["sessions"]
    by_sid = {s["sid"]: s for s in sess_list}
    assert set(by_sid["main"].keys()) == {"sid", "attached"}


def test_session_tree_lists_multiple_agents(tmp_path: Path) -> None:
    """Tier 2: each loaded agent appears once, in loaded_names() order."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("default")
    reg.get_or_load("alpha")

    names = [e["agent"] for e in reg.session_tree()]
    assert names == reg.loaded_names()
    assert set(names) == {"default", "alpha"}


def test_session_tree_lists_spawned_sessions(tmp_path: Path) -> None:
    """Tier 2: a spawned session shows up alongside 'main' under the same agent."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("default")
    sid = reg.spawn_session("default", "sub1")

    sids = {s["sid"] for s in reg.session_tree()[0]["sessions"]}
    assert {"main", sid} <= sids


def test_session_tree_sids_sorted(tmp_path: Path) -> None:
    """Tier 2: sessions within an agent are sorted by sid regardless of spawn order."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("default")
    reg.spawn_session("default", "zz")
    reg.spawn_session("default", "aa")

    sids = [s["sid"] for s in reg.session_tree()[0]["sessions"]]
    assert sids == sorted(sids)


def test_session_tree_all_false_when_nothing_attached(tmp_path: Path) -> None:
    """Tier 2: with no attached agent (fresh registry) every flag is False, at both
    the agent and session level."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("default")
    reg.spawn_session("default", "sub1")
    assert reg.attached_name is None           # public-surface precondition

    entry = reg.session_tree()[0]
    assert entry["attached"] is False
    assert all(s["attached"] is False for s in entry["sessions"])


def test_session_tree_returns_snapshot_copy(tmp_path: Path) -> None:
    """Tier 2: the returned structure is a snapshot — mutating it does not corrupt a
    second call (it must NOT hand out a handle to live registry state)."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("default")

    first = reg.session_tree()
    first[0]["agent"] = "MUTATED"
    first[0]["sessions"][0]["sid"] = "MUTATED"
    first.append({"bogus": True})

    by_name = {e["agent"]: e for e in reg.session_tree()}
    assert "default" in by_name, "second call still reflects live 'default'"
    by_sid = {s["sid"]: s for s in by_name["default"]["sessions"]}
    assert "main" in by_sid, "second call still reflects live 'main' session"
    assert all("bogus" not in e for e in by_name.values())
