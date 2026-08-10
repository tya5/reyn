"""Tier 2: proposal 0067 P0 — current_task / inbox-item attribute types (#3978).

Types and state only, no behaviour change (per the proposal's own P0
scope) — these tests verify the type contract exists and is constructible,
and that Session gains the new field WITHOUT anything reading or writing
it. The "no behaviour change" half of the claim is verified by the
existing session-attribution tests (test_session_dispatch_attribution.py
etc.) staying green untouched — this file does not re-test attribution.

Real Session instances throughout (no mocks) — the object under test for
the Session-integration checks is Session itself, so faking it would test
nothing real.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.runtime.task_types import CurrentTask, Requester
from reyn.runtime.transport import TuiRef
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path, *, agent_name: str = "test_agent") -> Session:
    return make_session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


# ---------------------------------------------------------------------------
# Requester — #2130's (agent, session_id) primitive, typed
# ---------------------------------------------------------------------------


def test_requester_holds_the_agent_session_pair() -> None:
    """Tier 2: Requester carries exactly the (agent_name, session_id) pair
    #2130 names as the addressing primitive."""
    r = Requester(agent_name="researcher", session_id="s-42")
    assert r.agent_name == "researcher"
    assert r.session_id == "s-42"


def test_requester_is_frozen() -> None:
    """Tier 2: Requester is immutable — an address, not mutable state (the
    same reasoning TransportRef's own variants are frozen for)."""
    r = Requester(agent_name="researcher", session_id="s-42")
    with pytest.raises(FrozenInstanceError):
        r.agent_name = "other"  # type: ignore[misc]


def test_requester_equality_is_by_value() -> None:
    """Tier 2: two Requesters naming the same pair compare equal — a
    dataclass default, but load-bearing here since P1 will use this as a
    dict/set key candidate for arbiter bookkeeping."""
    assert Requester(agent_name="a", session_id="s") == Requester(agent_name="a", session_id="s")
    assert Requester(agent_name="a", session_id="s") != Requester(agent_name="a", session_id="s2")


# ---------------------------------------------------------------------------
# CurrentTask — every field proposal 0067 § Types and substrate names
# ---------------------------------------------------------------------------


def test_current_task_defaults_are_all_none_except_wake() -> None:
    """Tier 2: a default-constructed CurrentTask carries no task — every
    field is None except `wake`, which defaults True per ADR-0040 D5
    ("a task's settle always wakes its issuer")."""
    t = CurrentTask()
    assert t.requester is None
    assert t.reply_to is None
    assert t.kind is None
    assert t.collect is None
    assert t.on_settle is None
    assert t.schema is None
    assert t.ttl_seconds is None
    assert t.wake is True


def test_current_task_holds_every_proposal_0067_field() -> None:
    """Tier 2: the load-bearing construction — every field the proposal's
    'Types and substrate' section names is settable and read back
    unchanged, including a real TransportRef (not a stand-in) for reply_to."""
    requester = Requester(agent_name="researcher", session_id="s-1")
    reply_to = TuiRef()
    t = CurrentTask(
        requester=requester,
        reply_to=reply_to,
        kind="prompt",
        collect="async",
        on_settle="deliver",
        schema="my_schema",
        ttl_seconds=300,
        wake=False,
    )
    assert t.requester == requester
    assert t.reply_to is reply_to
    assert t.kind == "prompt"
    assert t.collect == "async"
    assert t.on_settle == "deliver"
    assert t.schema == "my_schema"
    assert t.ttl_seconds == 300
    assert t.wake is False


def test_current_task_is_mutable() -> None:
    """Tier 2: unlike Requester, CurrentTask is NOT frozen — a task's own
    state changes over its lifetime once P1 wires this in (settling from
    running to delivered, etc.); P0 doesn't mutate it, but the type must
    allow it."""
    t = CurrentTask()
    t.kind = "pipeline"
    assert t.kind == "pipeline"


# ---------------------------------------------------------------------------
# Session integration — additive field, no behaviour change
# ---------------------------------------------------------------------------


def test_session_gains_current_task_defaulting_to_none(tmp_path: Path) -> None:
    """Tier 2: a freshly constructed Session carries `current_task = None`
    — the new field exists and is inert; nothing in construction populates
    it (that's P1's job)."""
    session = _make_session(tmp_path)
    assert session.current_task is None


def test_session_current_task_is_independently_settable(tmp_path: Path) -> None:
    """Tier 2: current_task is a real, externally-assignable attribute (not
    a read-only property standing in for something else) — proving the
    field is genuinely additive state, not a computed alias over
    _last_sender/_last_reply_to (which stay untouched, see module
    docstring)."""
    session = _make_session(tmp_path)
    task = CurrentTask(kind="exec")
    session.current_task = task
    assert session.current_task is task
    # _last_sender/_last_reply_to are the STILL-LIVE attribution path this
    # PR does not touch — setting current_task must not perturb them.
    assert session.last_sender() is None
