"""Tier 2: AgentRegistry.session_tree() — read-only snapshot accessor.

Exercises the public surface of ``session_tree()`` using a real AgentRegistry
and real AgentProfile instances (no mocks). Covers the shape contract,
attached-marking at both agent and session levels, and snapshot isolation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """Build a real AgentRegistry with a WAL but no sessions loaded."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )
    # Create a second agent profile so the registry has multiple on disk.
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    return reg


def test_session_tree_empty_when_no_sessions_loaded(tmp_path: Path) -> None:
    """Tier 2: session_tree() returns [] when no sessions are loaded."""
    reg = _make_registry(tmp_path)
    tree = reg.session_tree()
    assert tree == []


def test_session_tree_shape_with_attached_session(tmp_path: Path) -> None:
    """Tier 2: session_tree() entry has 'agent', 'attached', 'sessions' keys."""
    reg = _make_registry(tmp_path)
    # Manually inject a session record as _sessions uses dict keying.
    reg._sessions["default"] = {"main": object()}
    reg._attached = ("default", "main")

    tree = reg.session_tree()
    # There is exactly one entry for "default" — verify by name lookup, not count.
    by_name = {e["agent"]: e for e in tree}
    assert "default" in by_name
    entry = by_name["default"]
    assert set(entry.keys()) == {"agent", "attached", "sessions"}
    assert entry["attached"] is True
    assert isinstance(entry["sessions"], list)


def test_session_tree_session_entry_shape(tmp_path: Path) -> None:
    """Tier 2: each session entry in the tree has 'sid' and 'attached' keys."""
    reg = _make_registry(tmp_path)
    reg._sessions["default"] = {"main": object()}
    reg._attached = ("default", "main")

    tree = reg.session_tree()
    by_name = {e["agent"]: e for e in tree}
    sess_list = by_name["default"]["sessions"]
    # Verify the "main" session entry shape by looking it up by sid, not by index.
    by_sid = {s["sid"]: s for s in sess_list}
    assert "main" in by_sid
    sess = by_sid["main"]
    assert set(sess.keys()) == {"sid", "attached"}
    assert sess["attached"] is True


def test_session_tree_non_attached_agent_marked_false(tmp_path: Path) -> None:
    """Tier 2: agents that are not the attached one have attached=False."""
    reg = _make_registry(tmp_path)
    reg._sessions["default"] = {"main": object()}
    reg._sessions["alpha"] = {"main": object()}
    reg._attached = ("default", "main")

    tree = reg.session_tree()
    # Order follows loaded_names() which is insertion order of _sessions dict.
    by_name = {e["agent"]: e for e in tree}
    assert by_name["default"]["attached"] is True
    assert by_name["alpha"]["attached"] is False


def test_session_tree_non_attached_session_marked_false(tmp_path: Path) -> None:
    """Tier 2: sessions that are not the attached sid have attached=False."""
    reg = _make_registry(tmp_path)
    reg._sessions["default"] = {"main": object(), "sub1": object()}
    reg._attached = ("default", "main")

    tree = reg.session_tree()
    entry = tree[0]
    by_sid = {s["sid"]: s for s in entry["sessions"]}
    assert by_sid["main"]["attached"] is True
    assert by_sid["sub1"]["attached"] is False


def test_session_tree_attached_none_all_false(tmp_path: Path) -> None:
    """Tier 2: when no agent is attached (fresh registry), all attached flags are False."""
    reg = _make_registry(tmp_path)
    reg._sessions["default"] = {"main": object()}
    # A fresh registry has no attached agent — verify via the public accessor.
    assert reg.attached_name is None

    tree = reg.session_tree()
    by_name = {e["agent"]: e for e in tree}
    assert by_name["default"]["attached"] is False
    by_sid = {s["sid"]: s for s in by_name["default"]["sessions"]}
    assert by_sid["main"]["attached"] is False


def test_session_tree_sids_sorted(tmp_path: Path) -> None:
    """Tier 2: sessions within an agent are sorted by sid."""
    reg = _make_registry(tmp_path)
    # Insert in non-sorted order.
    reg._sessions["default"] = {"zz": object(), "aa": object(), "main": object()}
    reg._attached = None

    tree = reg.session_tree()
    sids = [s["sid"] for s in tree[0]["sessions"]]
    assert sids == sorted(sids)


def test_session_tree_returns_snapshot_copy(tmp_path: Path) -> None:
    """Tier 2: mutating the returned list/dicts does not affect a second call."""
    reg = _make_registry(tmp_path)
    reg._sessions["default"] = {"main": object()}
    reg._attached = ("default", "main")

    first = reg.session_tree()
    # Mutate the returned structure.
    first[0]["agent"] = "MUTATED"
    first[0]["sessions"][0]["sid"] = "MUTATED"
    first.append({"bogus": True})

    second = reg.session_tree()
    # The second call must reflect the live registry state, not the mutated copy.
    by_name = {e["agent"]: e for e in second}
    assert "default" in by_name, "second call still shows 'default' agent"
    by_sid = {s["sid"]: s for s in by_name["default"]["sessions"]}
    assert "main" in by_sid, "second call still shows 'main' session"
    # 'bogus' was appended to the first snapshot copy — it must not appear in the second.
    assert all("bogus" not in e for e in second)


def test_session_tree_order_follows_loaded_names(tmp_path: Path) -> None:
    """Tier 2: tree order matches loaded_names() insertion order."""
    reg = _make_registry(tmp_path)
    reg._sessions["default"] = {"main": object()}
    reg._sessions["alpha"] = {"main": object()}
    reg._attached = None

    tree = reg.session_tree()
    names_from_tree = [e["agent"] for e in tree]
    assert names_from_tree == reg.loaded_names()
