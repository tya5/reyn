"""Tier 2: #4387 Phase B ② (remaining consumers) — the read-model seam itself
(``ChatReadModel.load_older_conversation_history``), independent of the TUI
paging/search consumers ``test_4387_tui_paging_extends_from_disk.py`` drives
it through.

``RegistryReadModel`` — real ``AgentRegistry`` + real ``Session`` (durable
``history.jsonl``), no fakes: delegates to ``Session.extend_history_backward``
for the attached session (agent/session_id omitted) and for an EXPLICITLY
targeted, non-attached session (#3310 N2's own targeting shape,
``conversation_history``'s sibling contract this method mirrors).

``RemoteReadModel`` — the frame-sufficiency accept side: a remote client
holds no session and no on-disk history to extend into, so this always
degrades to ``0`` (never a fabricated count), regardless of what a caller
passes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.interfaces.repl.read_model import RegistryReadModel, RemoteReadModel
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _registry(tmp_path: Path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(
            agent_name=profile.name, snapshot_path=agent_dir / "state" / "snapshot.json",
        )
        s.load_history()
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    reg.create("beta")
    return reg


def _append_turns(s: Session, n: int) -> None:
    for i in range(n):
        s._append_history(ChatMessage(role="user", content=f"question {i}"))
        s._append_history(ChatMessage(role="assistant", content=f"answer {i}"))


@pytest.mark.asyncio
async def test_load_older_conversation_history_extends_the_attached_session(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: ``agent``/``session_id`` omitted -> the currently attached
    session (byte-identical targeting rule to ``conversation_history``'s own
    documented contract) — real disk-backed extension, real count returned."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        s = reg.get_session("alpha")
        assert s is not None
        _append_turns(s, 10)  # 20 durable messages
        s.history = s.history[-3:]

        rm = RegistryReadModel(reg)
        extended = rm.load_older_conversation_history()

        assert extended == 17, f"expected all 17 older messages, got {extended}"
        assert len(rm.conversation_history()) == 20, (
            "conversation_history() must reflect the extended (now-longer) log"
        )
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_load_older_conversation_history_targets_a_non_attached_session(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: #3310 N2's own targeting shape — ``agent`` (and optionally
    ``session_id``) resolve a session OTHER than the currently attached one,
    matching ``conversation_history``'s documented behavior exactly."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")  # "alpha" is attached...
        beta = reg.get_or_load("beta")  # ...but "beta" (loaded, not attached) is the target
        _append_turns(beta, 10)
        beta.history = beta.history[-3:]

        rm = RegistryReadModel(reg)
        extended = rm.load_older_conversation_history(agent="beta")

        assert extended == 17
        assert len(rm.conversation_history(agent="beta")) == 20
        # The ATTACHED session ("alpha") must be untouched by a call
        # explicitly targeting "beta".
        alpha = reg.get_session("alpha")
        assert alpha is not None
        assert alpha.history == [], (
            "the attached-but-not-targeted session must be untouched by a "
            f"call explicitly targeting a different agent: {alpha.history!r}"
        )
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_load_older_conversation_history_returns_zero_at_the_true_start(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: the caller's sole "nothing more exists" signal — a second
    call once the true start is reached returns 0, not a repeat count."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    try:
        await reg.attach("alpha")
        s = reg.get_session("alpha")
        assert s is not None
        _append_turns(s, 3)
        s.history = s.history[-2:]

        rm = RegistryReadModel(reg)
        first = rm.load_older_conversation_history()
        second = rm.load_older_conversation_history()

        assert first == 4
        assert second == 0
    finally:
        await reg.shutdown()


def test_remote_read_model_always_degrades_to_zero() -> None:
    """Tier 1: frame-sufficiency accept side — a remote client holds no
    session and no on-disk history to extend into; every call (targeted or
    not) degrades to 0, never a fabricated count."""
    rm = RemoteReadModel(transport=None)

    assert rm.load_older_conversation_history() == 0
    assert rm.load_older_conversation_history(agent="anything", session_id="main") == 0


def test_remote_read_model_split_methods_agree_with_conversation_history() -> None:
    """Tier 1: #5215 — ``RemoteReadModel`` carries NO override of
    ``resolve_conversation_history_source``/``conversation_history_from_
    source`` (unlike ``RegistryReadModel``, the one implementation with a
    real thread-safety concern to split for) — the base ``ChatReadModel``'s
    own default (delegate straight to ``conversation_history()``) is relied
    on silently. Explicit witness that the default produces the SAME
    frame-sufficiency degrade (``[]``) as calling ``conversation_history()``
    directly, so a future base-class default change is caught here rather
    than only by this class's silence (six-questions ④)."""
    rm = RemoteReadModel(transport=None)

    assert rm.resolve_conversation_history_source() == []
    assert rm.conversation_history_from_source([]) == []
    assert (
        rm.conversation_history_from_source(rm.resolve_conversation_history_source())
        == rm.conversation_history()
        == []
    )
