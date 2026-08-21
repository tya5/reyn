"""Tier 1/2: #4996 — ``ChatReadModel`` implementations declare which of
their degradable reads they actually support, so a caller can tell "nothing
to show" apart from "this client can never show this".

Owner-directed via architect ("capability declaration → issue → leader"),
same discipline as :class:`~reyn.security.sandbox.backend.
AxisEnforcementDeclaration` and :mod:`~reyn.core.dispatch.
content_declarations` — a 3rd example, not a new concept. ``None``/``[]``/
``0``/``False`` remain the correct graceful-degrade return values; this
declaration changes nothing about them.

Two witnesses (both required — lead-coder/architect co-vet: ①alone would
pass an "declared but nobody reads it" implementation, the #4991 shape):

①  A new :class:`~reyn.interfaces.repl.read_model.ChatReadModel`
   implementation that omits one of :class:`~reyn.interfaces.repl.
   read_model.ChatReadModelCapabilities`'s 6 required fields fails to
   CONSTRUCT — a bare dataclass-completeness check, scoped exactly as
   ``ChatReadModelCapabilities``'s own docstring discloses (catches a
   missing FIELD among the 6 already declared, not a missing declaration
   for some 7th method that might be added later).
②  The one real call site architect/lead-coder picked (most user-visible
   harm): :meth:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp.
   _apply_hydrated_messages`. An empty ``conversation_history()`` read
   renders DIFFERENTLY depending on the declaration — unsupported gets an
   explicit marker row, a genuinely empty but CAPABLE read stays exactly as
   silent as it always has (the accept-side half of the same test family,
   so an "always show the marker" implementation cannot pass vacuously
   either).

Real ``AgentRegistry`` + real ``Session`` + real ``RegistryReadModel`` +
real ``RemoteReadModel`` (backed by a real, minimal ``ClientTransport``) +
the real mounted ``TextualChatApp`` — no mocks.
"""
from __future__ import annotations

import asyncio
from dataclasses import fields
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.app import HISTORY_UNAVAILABLE_MARKER
from reyn.interfaces.repl.read_model import (
    ChatReadModel,
    ChatReadModelCapabilities,
    RegistryReadModel,
    RemoteReadModel,
)
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


class QueueTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` whose ``frames()`` drains
    an ``asyncio.Queue`` the test pushes onto (mirrors ``test_4983_
    session_switch_off_thread.py``'s own helper of the same name/shape).
    Not exercised for its frame stream here — only as the real collaborator
    :class:`RemoteReadModel` is constructed with."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue" = asyncio.Queue()

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> str:
        return ""

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None
    ) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self):
        return None

    def put_display(self, msg: OutboxMessage) -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> str:  # pragma: no cover - trivial
        return ""

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


# ── witness① — dataclass completeness ───────────────────────────────────


def test_capabilities_dataclass_rejects_a_missing_field():
    """Tier 1: omitting any one of the 6 required fields fails to
    CONSTRUCT — a ``TypeError``, not a silently-defaulted ``False``. Mirrors
    ``AxisEnforcementDeclaration``'s own witness verbatim."""
    with pytest.raises(TypeError):
        ChatReadModelCapabilities(  # type: ignore[call-arg]
            completion_session=True,
            intervention_head=True,
            pending_command_ui=True,
            has_command_ui_region=True,
            conversation_history=True,
            # load_older_conversation_history omitted
        )


def test_capabilities_dataclass_has_exactly_the_6_declared_fields():
    """Tier 1: pins the declared vocabulary itself — the 6 names #4996's
    own issue enumerated, no more, no fewer. A regression here is a
    silent widening/narrowing of what's declared, not a missing-field bug
    (that's the test above)."""
    names = {f.name for f in fields(ChatReadModelCapabilities)}
    assert names == {
        "completion_session",
        "intervention_head",
        "pending_command_ui",
        "has_command_ui_region",
        "conversation_history",
        "load_older_conversation_history",
    }


def test_a_new_chat_read_model_that_omits_capabilities_fails_to_construct():
    """Tier 1: the OTHER half of witness① — a new ``ChatReadModel``
    subclass that forgets to implement the abstract ``capabilities``
    property fails at construction (Python's own ABC mechanism), not at
    first use. Mirrors the class docstring's own "abstract so a partial
    implementation fails at construction" rule, extended to this new
    property exactly like every other accessor on the class."""

    class _IncompleteReadModel(ChatReadModel):
        def snapshot(self, config=None):
            return None

        def intervention_head(self):
            return None

        def pending_command_ui(self):
            return None

        def clear_pending_command_ui(self) -> None:
            return None

        @property
        def has_command_ui_region(self) -> bool:
            return False

        @property
        def history_path(self) -> Path:
            return Path("/tmp/unused")

        def conversation_history(self, *, limit=None, agent=None, session_id=None):
            return []

        def load_older_conversation_history(self, *, agent=None, session_id=None) -> int:
            return 0

        # ``capabilities`` deliberately NOT implemented.

    with pytest.raises(TypeError):
        _IncompleteReadModel()  # type: ignore[abstract]


# ── witness② — the actual render site ───────────────────────────────────


def _registry(tmp_path: Path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        s = make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )
        s.load_history()
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


async def _settle(pilot, n: int = 2) -> None:
    for _ in range(n):
        await pilot.pause()


def _flow_rows(app: TextualChatApp) -> "list[tuple[str, str]]":
    return [(e.item.kind, e.item.text) for e in app.query_one(FlowView).entries]


@pytest.mark.asyncio
async def test_capability_declared_unsupported_and_empty_shows_the_marker(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: witness②'s own falsifier. A read model that DECLARES it can
    never produce conversation history (``RemoteReadModel`` — a real
    instance, frame-sufficiency boundary) and whose read comes back empty
    gets an explicit marker row, not silence — reverting the
    ``capabilities`` check in ``_apply_hydrated_messages`` (leaving the pane
    silently blank) turns this red."""
    monkeypatch.chdir(tmp_path)
    transport = QueueTransport()
    read_model = RemoteReadModel(transport)
    assert read_model.capabilities.conversation_history is False
    assert read_model.conversation_history() == []

    app = TextualChatApp(transport=transport, read_model=read_model, agent_name="alpha")
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        rows = _flow_rows(app)

    assert (
        "system",
        HISTORY_UNAVAILABLE_MARKER,
    ) in rows, "an unsupported+empty read must render the marker, not silence"


@pytest.mark.asyncio
async def test_capability_declared_supported_and_empty_stays_silent(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: accept-side — the OTHER required direction (co-vet note: ①
    alone would pass an "always show the marker" implementation too). A
    genuinely NEW, fully-capable local session with no history yet must NOT
    show the marker: an empty conversation pane at first mount is the
    correct, ordinary "nothing to restore" case, unchanged by #4996."""
    monkeypatch.chdir(tmp_path)
    reg = _registry(tmp_path)
    await reg.attach("alpha")
    read_model = RegistryReadModel(reg)
    assert read_model.capabilities.conversation_history is True
    assert read_model.conversation_history() == []

    transport = QueueTransport()
    app = TextualChatApp(transport=transport, read_model=read_model, agent_name="alpha")
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(pilot)
        rows = _flow_rows(app)

    assert HISTORY_UNAVAILABLE_MARKER not in [text for _, text in rows], (
        "a genuinely empty but CAPABLE read must stay silent, unchanged "
        "from pre-#4996 behavior"
    )
