"""Tier 2: #3593 ① — a session's live capability envelope is PRESERVED, never
overwritten, when ``reapply_visibility_override`` cannot read a base.

``CapabilityVisibility.reapply_visibility_override`` re-resolves the WHOLE agent
envelope from base and SETs both live fields (SET, not union — that is what lets a
``/visibility`` toggle-ON restore a capability *up to* the envelope without
re-widening past it). The SET is correct only when a base was actually obtained:
without an envelope source it used to compose the override against a default
``ContextualPermission()`` (allows everything) and SET that, replacing the topology
bindings, the #2081 delegate floor and the #2103-S1a per-session narrowing with
"allow-all minus whatever the operator toggled". A missing input triggered a write,
and the write went outward.

The reach path exercised here is the measured one from the issue: the restart path
(``AgentRegistry.spawn_session`` with an existing sid) injects the sid-keyed
narrowing and then calls ``load_persisted_toggles()``, which calls
``reapply_visibility_override`` for any sid with a persisted ``visibility.yaml``.
No ``refresh_config_projections()`` is involved.

Real ``AgentRegistry`` + real ``Session`` throughout; the two arms differ in exactly
one input — whether the session factory hands the session its registry
back-reference. Each arm asserts VALUES on its own (which tool names the live gate
denies), never "the two arms agree": both arms sharing one defect would agree.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.security.permissions.effective import CapabilityAxis, ContextualLayer
from tests._support.agent_session import make_session

NARROWED_TOOL = "write_file"   # denied by the sid's persisted config.yaml (the envelope)
TOGGLED_TOOL = "read_file"     # hidden by the sid's persisted visibility.yaml (the override)


def _make_registry(tmp_path: Path, *, with_back_reference: bool) -> AgentRegistry:
    """A real registry whose sessions carry (or do not carry) the registry
    back-reference — the single variable between the two arms. ``registry=None`` is
    the shape production's ``build_scoped_chat_session`` produces when its caller
    passes no registry (``interfaces/cli/commands/dogfood.py``'s bootstrap window)."""
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name,
            state_log=state_log,
            registry=holder.get("reg") if with_back_reference else None,
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not (tmp_path / ".reyn" / "agents" / "alice").exists():
        AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


def _denies_tool(session: Session, name: str) -> bool:
    """What the LIVE gate does — the single field the RouterLoop's advertisement filter
    and its call-time gate both read."""
    return not ContextualLayer(session.contextual_permission).allows(CapabilityAxis.TOOL, name)


def _persist_narrowing_and_toggle(tmp_path: Path) -> str:
    """Produce the on-disk precondition the issue measured: one sid with BOTH a
    ``config.yaml`` narrowing (the envelope layer) and a ``visibility.yaml`` toggle
    (the override layer). Written by a normal, fully-wired session — so the state the
    restart arms read back is real persisted state, not a hand-built fixture."""
    reg = _make_registry(tmp_path, with_back_reference=True)
    reg.get_or_load("alice")
    sid = reg.spawn_session(
        "alice",
        narrowing={"tool_deny": [NARROWED_TOOL]},
        presentation_consumer=None,
        intervention_bridge=None,
    )
    reg.get_session("alice", sid).set_capability_visible("tool", TOGGLED_TOOL, False)
    return sid


def _restart(tmp_path: Path, sid: str, *, with_back_reference: bool) -> Session:
    """The restart path (``restore_all`` re-enters an existing sid): a fresh registry
    re-creates (alice, sid), which re-injects the sid's persisted narrowing and then
    fires ``load_persisted_toggles()`` -> ``reapply_visibility_override``."""
    reg = _make_registry(tmp_path, with_back_reference=with_back_reference)
    reg.get_or_load("alice")
    reg.spawn_session("alice", sid=sid, presentation_consumer=None, intervention_bridge=None)
    return reg.get_session("alice", sid)


def test_envelope_narrowing_survives_a_reapply_with_no_base(tmp_path, monkeypatch) -> None:
    """Tier 2: with NO envelope source, the re-resolve preserves the live envelope —
    the sid's persisted ``tool_deny`` is still enforced by the live gate after
    ``reapply_visibility_override`` ran. RED before #3593 ①: the re-resolve composed
    the override against an allow-everything default and SET it, so the injected
    narrowing was silently replaced and the tool became allowed."""
    monkeypatch.chdir(tmp_path)
    sid = _persist_narrowing_and_toggle(tmp_path)

    session = _restart(tmp_path, sid, with_back_reference=False)

    assert _denies_tool(session, NARROWED_TOOL), (
        f"the sid's persisted envelope narrowing must still deny {NARROWED_TOOL!r} after a "
        "re-resolve that could not read a base — with no base there is no standing to "
        "overwrite the live envelope"
    )
    # The named cost of preserving (#3593 ①), asserted rather than left implicit: the
    # override cannot be composed without an envelope to compose it against, so the
    # toggle is recorded and persisted but does not reach the live gate on such a
    # session. Applying it would require inventing the envelope, which is the defect.
    assert not _denies_tool(session, TOGGLED_TOOL), (
        "with no base the override is NOT applied to the live gate (preserve, not partially "
        "recompose) — it stays visible in the override/persisted state instead"
    )
    assert {"kind": "tool", "name": TOGGLED_TOOL} in (
        session.capability_visibility_state()["hidden_by_session"]
    ), "the toggle itself is still recorded and reported, only not applied to the live gate"


def test_reapply_with_a_base_still_composes_envelope_and_override(tmp_path, monkeypatch) -> None:
    """Tier 2: the preserve branch does not swallow the normal path — with an envelope
    source, the same restart resolves the base AND composes the override, so the live
    gate denies both the envelope-narrowed tool and the session-hidden one. Values are
    named here directly; this arm never compares itself against the no-base arm."""
    monkeypatch.chdir(tmp_path)
    sid = _persist_narrowing_and_toggle(tmp_path)

    session = _restart(tmp_path, sid, with_back_reference=True)

    assert _denies_tool(session, NARROWED_TOOL), (
        f"the envelope's persisted narrowing must deny {NARROWED_TOOL!r}"
    )
    assert _denies_tool(session, TOGGLED_TOOL), (
        f"the session's persisted /visibility override must deny {TOGGLED_TOOL!r}"
    )


def test_the_preserve_branch_is_reached_on_the_restart_path(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: the preserve branch is REACHED through the production restart path, and
    says so. A branch nothing reaches is dead, and a preserved envelope is
    indistinguishable from a correctly re-resolved one by looking at the envelope alone
    — the WARNING is the only thing that distinguishes them, which is why preserving is
    not allowed to be silent. RED if the branch stops being reached, or if it is reached
    silently."""
    monkeypatch.chdir(tmp_path)
    sid = _persist_narrowing_and_toggle(tmp_path)

    with caplog.at_level(logging.WARNING, logger="reyn.runtime.capability_visibility"):
        _restart(tmp_path, sid, with_back_reference=False)

    unreadable = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "#3593" in r.getMessage()
    ]
    assert unreadable, (
        "reaching the preserve branch must surface: no WARNING naming #3593 was emitted "
        "while re-applying the visibility override on a session with no envelope source"
    )
    message = unreadable[0].getMessage()
    assert "alice" in message, "the warning must name the agent whose envelope was not re-resolved"
    assert sid in message, "the warning must name the session it happened on"


def test_a_wired_session_does_not_reach_the_preserve_branch(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: the branch is reached ONLY when the base is genuinely unavailable — the
    same restart with an envelope source emits no such warning. Without this, a warning
    fired unconditionally would satisfy the reachability witness above while telling an
    operator nothing."""
    monkeypatch.chdir(tmp_path)
    sid = _persist_narrowing_and_toggle(tmp_path)

    with caplog.at_level(logging.WARNING, logger="reyn.runtime.capability_visibility"):
        _restart(tmp_path, sid, with_back_reference=True)

    assert not [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "#3593" in r.getMessage()
    ], "a session that CAN read its base must not report an unreadable envelope source"
