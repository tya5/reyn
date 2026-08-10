"""Tier 2: #3615 — the READ-model twin of #3593. When
``CapabilityVisibility.capability_visibility_state`` cannot read an envelope base
(no ``registry`` back-reference — the same condition #3593 ① preserves the live
gate on rather than widen), the pre-fix code composed against
``ContextualLayer(None)`` (which is (top), allows everything) and reported every
tool/mcp/category row as ``authorized`` — an absent input rendered as a
permissive answer, exactly the write-side defect's shape on the read side.

The fix: such rows are reported under a new ``unknown`` bucket, with a top-level
``envelope_unknown=True`` flag, rather than folded into ``authorized``.

Real ``AgentRegistry`` + real ``Session`` throughout (mirrors
``test_3593_preserve_envelope_when_base_unreadable.py``'s harness exactly — same
two arms, same single varying input). Each arm asserts VALUES on its own; "the
two arms agree" is never used alone, since both sharing one defect would agree.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

NARROWED_TOOL = "write_file"  # denied by the sid's persisted config.yaml (the envelope)


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


def _spawn_narrowed(tmp_path: Path, *, with_back_reference: bool) -> Session:
    """A session spawned with a real envelope narrowing (``tool_deny:
    [write_file]``) — real construction, not a hand-built input. The narrowing is
    identical across both arms; only the registry back-reference differs."""
    reg = _make_registry(tmp_path, with_back_reference=with_back_reference)
    reg.get_or_load("alice")
    sid = reg.spawn_session(
        "alice",
        narrowing={"tool_deny": [NARROWED_TOOL]},
        presentation_consumer=None,
        intervention_bridge=None,
    )
    return reg.get_session("alice", sid)


def test_read_model_reports_unknown_not_authorized_with_no_base(tmp_path, monkeypatch) -> None:
    """Tier 2: with NO envelope source, the read model must NOT report the
    envelope-narrowed tool as ``authorized`` — it belongs in ``unknown``, and
    ``envelope_unknown`` must be True. RED before #3615: the narrowed tool showed up
    in ``authorized`` (composed against an allow-everything default) and
    ``denied_by_envelope`` was empty, which a consumer reads as "nothing is
    denied" — a wrong, confident answer to "is this tool authorized?"."""
    monkeypatch.chdir(tmp_path)
    session = _spawn_narrowed(tmp_path, with_back_reference=False)

    state = session.capability_visibility_state()

    assert state["envelope_unknown"] is True, (
        "a session with no envelope source must report envelope_unknown=True"
    )
    authorized_tools = {i["name"] for i in state["authorized"] if i["kind"] == "tool"}
    unknown_tools = {i["name"] for i in state["unknown"] if i["kind"] == "tool"}
    assert NARROWED_TOOL not in authorized_tools, (
        f"{NARROWED_TOOL!r} must not be reported authorized when the envelope that "
        "narrows it could not be read — that is reporting 'unknown' as 'permitted'"
    )
    assert NARROWED_TOOL in unknown_tools, (
        f"{NARROWED_TOOL!r} must be reported as unknown (undetermined), not silently "
        "dropped or misclassified"
    )
    assert state["denied_by_envelope"] == [], (
        "with no base, denial cannot be honestly asserted either — denied_by_envelope "
        "must stay empty, not fabricate a denial"
    )


def test_read_model_reports_authorized_and_denied_correctly_with_a_base(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the unknown branch does not swallow the normal path — with an
    envelope source, the SAME narrowed spawn reports the narrowed tool as
    ``denied_by_envelope`` (not authorized, not unknown), and an unnarrowed tool as
    authorized. Values are named directly; this arm never compares itself against
    the no-base arm."""
    monkeypatch.chdir(tmp_path)
    session = _spawn_narrowed(tmp_path, with_back_reference=True)

    state = session.capability_visibility_state()

    assert state["envelope_unknown"] is False
    authorized_tools = {i["name"] for i in state["authorized"] if i["kind"] == "tool"}
    denied_tools = {i["name"] for i in state["denied_by_envelope"] if i["kind"] == "tool"}
    assert NARROWED_TOOL in denied_tools, f"{NARROWED_TOOL!r} must be reported denied by envelope"
    assert NARROWED_TOOL not in authorized_tools
    assert "list_agents" in authorized_tools, "an unnarrowed tool must still be authorized"
    assert state["unknown"] == [], "a session with a readable base has nothing unknown"


def test_the_unknown_branch_is_reached_on_the_restart_path(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: the unknown branch is REACHED through a real construction (spawn with
    no registry back-reference), and says so. A branch nothing reaches is dead, and
    an 'unknown' read is indistinguishable from a correctly-resolved-and-empty one by
    looking at the lists alone — the WARNING is what distinguishes them. RED if the
    branch stops being reached, or is reached silently."""
    monkeypatch.chdir(tmp_path)
    session = _spawn_narrowed(tmp_path, with_back_reference=False)

    with caplog.at_level(logging.WARNING, logger="reyn.runtime.capability_visibility"):
        state = session.capability_visibility_state()

    assert state["envelope_unknown"] is True
    fired = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "#3615" in r.getMessage()
    ]
    assert fired, (
        "reaching the unknown branch must surface: no WARNING naming #3615 was "
        "emitted while reading capability_visibility_state() on a session with no "
        "envelope source"
    )
    message = fired[0].getMessage()
    assert "alice" in message, "the warning must name the agent whose envelope was not resolved"


def test_a_wired_session_does_not_reach_the_unknown_branch(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: the branch is reached ONLY when the base is genuinely unavailable —
    the same read on a session WITH an envelope source emits no such warning and
    reports no unknown rows. Without this, a warning fired unconditionally would
    satisfy the reachability witness above while telling an operator nothing."""
    monkeypatch.chdir(tmp_path)
    session = _spawn_narrowed(tmp_path, with_back_reference=True)

    with caplog.at_level(logging.WARNING, logger="reyn.runtime.capability_visibility"):
        state = session.capability_visibility_state()

    assert state["unknown"] == []
    assert not [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "#3615" in r.getMessage()
    ], "a session that CAN read its base must not report an unreadable envelope source"
