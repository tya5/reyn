"""Tier 2: FP-0043 S4b-2 — web-transport session routing (chainlit-free helper).

The web routing-key glue, unit-tested WITHOUT the optional chainlit dependency:
a per-browser thread id maps to its own ``web:<thread>`` Session of an agent,
two threads are isolated, and a missing thread id falls back to ``web:default``
(inside the web namespace, never merged into the REPL's "main"). The actual
chainlit output-drain isolation is verified by the CI integration test; here we
pin the mapping contract the app.py glue depends on.

Falsification (feedback_falsify_acceptance_test_before_proof): the fallback test
reds if web_native_id stops defaulting; isolation reds if the routing-key stops
namespacing by thread (both checked in the companion comments).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import Session
from reyn.core.events.state_log import StateLog
from reyn.interfaces.chainlit_app.web_routing import (
    resolve_web_session,
    web_native_id,
    web_session_id,
)


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")
        return s

    return AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def test_web_native_id_and_session_id_fallback():
    """Tier 2: a missing/blank thread id falls back to web:default (not "main")."""
    assert web_native_id("T123") == "T123"
    assert web_native_id(None) == "default"          # absent → default
    assert web_native_id("") == "default"
    assert web_native_id("   ") == "default"          # blank → default
    assert web_session_id("T123") == "web:T123"
    assert web_session_id(None) == "web:default"      # namespaced, never "main"


@pytest.mark.asyncio
async def test_resolve_web_session_maps_thread_to_its_own_session(tmp_path):
    """Tier 2: a browser thread resolves to its own web:<thread> session, live."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")

    s = resolve_web_session(reg, "alpha", "T1")
    assert s is reg.get_session("alpha", "web:T1")    # mapped by routing-key
    assert s.is_attached is True                      # browser thread is live
    # idempotent: same thread resumes the same session, not a new one.
    assert resolve_web_session(reg, "alpha", "T1") is s


@pytest.mark.asyncio
async def test_resolve_web_session_threads_are_isolated(tmp_path):
    """Tier 2: distinct browser threads get distinct sessions (no cross-talk)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")

    a = resolve_web_session(reg, "alpha", "T1")
    b = resolve_web_session(reg, "alpha", "T2")
    assert a is not b
    assert reg.get_session("alpha", "web:T1") is a
    assert reg.get_session("alpha", "web:T2") is b


@pytest.mark.asyncio
async def test_resolve_web_session_outboxes_are_independent(tmp_path):
    """Tier 2: the output-isolation invariant — each web thread's Session has its
    OWN outbox, so two browser tabs never cross-leak.

    This is the chainlit-free core of the per-browser output drain: app.py binds
    each browser's drain loop to ITS resolved session's ``.outbox`` (vs the old
    single shared ``repl_outbox`` that all tabs raced). Verifying the outboxes are
    independent queues proves the no-cross-leak guarantee at the source; the
    chainlit ``cl.Message.send`` rendering itself is unchanged. (chainlit is not in
    CI — an app.py importorskip integration test would silently skip, so the
    isolation is pinned HERE instead of behind a skip.)
    """
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")

    a = resolve_web_session(reg, "alpha", "T1")
    b = resolve_web_session(reg, "alpha", "T2")
    assert a.outbox is not b.outbox                   # distinct queues
    a.outbox.put_nowait(object())                     # an output for tab T1
    assert a.outbox.empty() is False
    assert b.outbox.empty() is True                   # tab T2 sees nothing of T1's


@pytest.mark.asyncio
async def test_resolve_web_session_missing_thread_uses_web_default(tmp_path):
    """Tier 2: a missing thread id resolves to web:default, distinct from a real
    thread AND never the REPL's "main"."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")

    d = resolve_web_session(reg, "alpha", None)
    assert d is reg.get_session("alpha", "web:default")   # falls back inside web ns
    assert reg.get_session("alpha", "main") is not d       # NOT merged into main
    real = resolve_web_session(reg, "alpha", "T1")
    assert real is not d                                   # distinct from a real thread
