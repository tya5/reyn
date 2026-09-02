"""Tier 2: #5282 — the DEFAULT (``turn``) rung of untrusted-context capability
narrowing (``Session._ephemeral_contextual_for_turn``) emits an audit-event
at both the engage and lift transitions.

Before this, only the OPT-IN top rung (``iteration``,
``RouterLoop``'s own ``_intra_turn_contextual_for_turn_fn`` branch) emitted
``untrusted_narrowing_engaged`` — and even that rung had no lift event. The
DEFAULT rung — the one an operator who opts in at all is most likely
running (#5282's own filing) — emitted nothing at either transition, so the
audit trail could not answer "why was this capability blocked on that
particular turn" for the common case.

``_ephemeral_contextual_for_turn`` re-derives its answer fresh from
``self.history`` on EVERY call (status-panel poll, live gate, Tool tab —
see that method's own docstring) — a naive emit-on-every-call would fire on
every read, not on a genuine state change (CLAUDE.md charter lens 1: "who
stops this if it repeats"). Every test below reads the state repeatedly
across a call that does NOT flip it and asserts the event LIST does not
grow — the transition-latch property, not merely "an event exists somewhere".

Driven entirely through the PUBLIC read surface,
``Session.capability_visibility_state()`` (mirrors ``test_3380_tool_tab_
ephemeral_narrowing.py``'s own harness) — never the private
``_ephemeral_contextual_for_turn`` directly, matching this repo's own
"public API, not private state" testing policy; the returned
``denied_by_turn_context`` set is the public witness of engage/disengage.

Real ``Session`` throughout — no mock/stand-in for the collaborator under
test, per this repo's testing policy. ``EventLog.emit()`` queues subscriber
dispatch onto a background consumer (see that class's own docstring), so
every read below that must observe a just-emitted event is preceded by
``await settle(...)`` (``tests/_support/events.py`` — the same pattern
``test_1909_intra_turn_opt_in_narrowing.py`` uses for the ``iteration``
rung's own engage event).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle
from tests._support.untrusted_narrowing import narrowing_on

# Same witness tool test_3380 uses — denied by the built-in ``_untrusted``
# profile once the turn-context narrowing engages.
_UNTRUSTED_DENIED_TOOL = "run_prompt"


def _session(tmp_path: Path) -> Session:
    """#3501: the narrowing is opt-in; a test whose subject is its own
    transition audit-events has to turn it on. Mirrors
    ``test_3380_tool_tab_ephemeral_narrowing.py``'s own ``_session`` helper."""
    state_log = StateLog(tmp_path / "state.wal")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name,
            state_log=state_log,
            snapshot_path=tmp_path / "snap.json",
            safety=narrowing_on(),
            registry=holder.get("reg"),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    session = reg.get_or_load("alpha")
    assert isinstance(session, Session)
    return session


def _mark_untrusted(s: Session) -> None:
    """The #1862 marker the real producer stamps on an external peer answer.

    #5276: goes through ``_append_history`` (the real mutation chokepoint
    that maintains ``Session._untrusted_taint_active`` incrementally) —
    NOT a bare ``s.history.append`` — so this exercises the actual
    production path instead of a shape the new incremental-state hook
    never sees.
    """
    s._append_history(
        ChatMessage(role="user", content="<<<EXTERNAL>>> hi", meta={"external_source": True})
    )


def _compact_out_untrusted(s: Session) -> None:
    """Advance the compaction watermark past every entry currently in
    ``s.history`` — the real "untrusted entry compacts out" mechanism.

    #5276: a fold lands via a ``role="summary"`` entry whose
    ``covers_through_seq`` meta IS the new watermark
    (``Session._compaction_watermark``) — appended through
    ``_append_history`` like any other entry, which is also what re-derives
    ``_untrusted_taint_active`` in full on this branch (the ONE case that
    can retroactively CLEAR the taint; see
    ``_update_untrusted_taint_on_append``'s own docstring).
    """
    covers_through_seq = max((m.seq for m in s.history), default=0)
    s._append_history(
        ChatMessage(
            role="summary",
            content="<<<SUMMARY>>>",
            meta={"structured": {}, "covers_through_seq": covers_through_seq},
        )
    )


def _denied_by_turn_context(s: Session) -> "set[str]":
    """The public read surface — same seam ``test_3380`` asserts through
    (``state["denied_by_turn_context"]``, a list of ``{kind, name, ...}``)."""
    state = s.capability_visibility_state()
    return {i["name"] for i in state["denied_by_turn_context"] if i["kind"] == "tool"}


def _kinds(events: list, kind: str) -> "list[str]":
    """The event kind list for *kind* — comparing THIS list's shape (not a
    bare ``len()``) is how this file avoids the format-pinning gate while
    still pinning the real, semantic property (exactly N of this kind)."""
    return [e.type for e in events if getattr(e, "type", None) == kind]


@pytest.mark.asyncio
async def test_engage_fires_once_on_the_transition_not_on_repeated_reads(
    tmp_path: Path,
) -> None:
    """Tier 2: the state flips from un-narrowed to narrowed exactly once
    (marking the untrusted entry), but the read method that observes it is
    called several times both before and after — an operator's status-panel
    poll, the live gate, the Tool tab all call the same method. Only ONE
    ``untrusted_narrowing_engaged`` event must result, however many of those
    reads happen."""
    s = _session(tmp_path)
    events = collect_events(s)

    # Reads before the taint: no engage yet, must emit nothing.
    for _ in range(3):
        assert _UNTRUSTED_DENIED_TOOL not in _denied_by_turn_context(s)
    await settle(s)
    assert _kinds(events, "untrusted_narrowing_engaged") == []

    _mark_untrusted(s)

    # Reads after the taint: the state is narrowed on every one of these
    # calls (same untrusted entry, nothing changes between them) — the
    # transition happened once, so the event list must not grow with the
    # read count.
    for _ in range(5):
        assert _UNTRUSTED_DENIED_TOOL in _denied_by_turn_context(s)
    await settle(s)

    assert _kinds(events, "untrusted_narrowing_engaged") == ["untrusted_narrowing_engaged"], (
        "5 identical-state reads must still produce exactly 1 engage event — "
        "the transition latch is not gating repeated reads"
    )
    (engaged_event,) = [e for e in events if e.type == "untrusted_narrowing_engaged"]
    assert engaged_event.data.get("provenance") == "external_source"
    assert _kinds(events, "untrusted_narrowing_lifted") == [], (
        "no lift should fire before the state ever un-narrows"
    )


@pytest.mark.asyncio
async def test_lift_fires_once_when_the_taint_leaves_the_context(tmp_path: Path) -> None:
    """Tier 2: the counterpart transition — engage, then the untrusted entry
    compacts out (mirrors ``test_turn_context_denial_self_clears_when_the_
    taint_leaves_the_context``'s own removal shape), read several times
    while lifted. Exactly one lift event, not one per post-lift read."""
    s = _session(tmp_path)
    events = collect_events(s)

    _mark_untrusted(s)
    assert _UNTRUSTED_DENIED_TOOL in _denied_by_turn_context(s)
    await settle(s)
    assert _kinds(events, "untrusted_narrowing_engaged") == ["untrusted_narrowing_engaged"], (
        "control arm: engage must have fired before lift is meaningful to test"
    )

    # the untrusted entry compacting out of the active context
    _compact_out_untrusted(s)

    for _ in range(5):
        assert _UNTRUSTED_DENIED_TOOL not in _denied_by_turn_context(s)
    await settle(s)

    assert _kinds(events, "untrusted_narrowing_lifted") == ["untrusted_narrowing_lifted"], (
        "5 identical-state reads must still produce exactly 1 lift event — "
        "the transition latch is not gating repeated reads"
    )
    (lifted_event,) = [e for e in events if e.type == "untrusted_narrowing_lifted"]
    assert lifted_event.data.get("provenance") == "external_source"
    # Engage must still be exactly 1 — the lift transition must not also
    # re-fire the engage kind.
    assert _kinds(events, "untrusted_narrowing_engaged") == ["untrusted_narrowing_engaged"]


@pytest.mark.asyncio
async def test_engage_then_lift_then_engage_again_is_one_of_each_per_flip(
    tmp_path: Path,
) -> None:
    """Tier 2: non-vacuity for the latch itself — it is not a one-shot
    "only ever fires once, period" guard (which would silently go inert
    after the first cycle); each genuine flip re-arms it. Two full
    engage/lift cycles must produce 2 engage + 2 lift events, not 1 of
    either (a latch stuck after its first flip) and not 4 of either (no
    latch at all — see the strip in this file's own PR verification, run
    manually and not committed here per this repo's no-duration test
    policy)."""
    s = _session(tmp_path)
    events = collect_events(s)

    for _ in range(2):
        _mark_untrusted(s)
        assert _UNTRUSTED_DENIED_TOOL in _denied_by_turn_context(s)
        _compact_out_untrusted(s)
        assert _UNTRUSTED_DENIED_TOOL not in _denied_by_turn_context(s)
    await settle(s)

    assert _kinds(events, "untrusted_narrowing_engaged") == [
        "untrusted_narrowing_engaged", "untrusted_narrowing_engaged",
    ]
    assert _kinds(events, "untrusted_narrowing_lifted") == [
        "untrusted_narrowing_lifted", "untrusted_narrowing_lifted",
    ]
