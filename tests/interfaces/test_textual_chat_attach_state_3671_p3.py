"""Tier 2: the Textual chat TUI consumes `has_session()`/`attach_failed()` to
distinguish "not yet attached" from a real value, and blocks submission until
attached (#3671 P3).

Before this PR, `interfaces/inline/` had ZERO references to `has_session()` —
the header rendered `model — │ agent <name> │ cost $0.0000 │ ctx —` whether
nothing was attached yet OR the attach had genuinely failed, indistinguishable
from each other and from a real (if unlikely) `$0.0000` cost. The composer
was worse: `on_composer_submitted` cleared the typed text UNCONDITIONALLY
before checking anything, so a message typed while `has_session()` was still
`False` (#3671 P2's own new window) was silently discarded — the opposite of
decision 4B ("keep text in the box").

B0 (owner ruling: one shared mechanism before B1/B2, so the header and the
composer can never disagree) is `TextualChatApp._attach_state()` — a tri-state
`"connecting" | "failed" | None` read off the SAME
`transport.has_session()` / `transport.attach_failed()` pair both consumers
now use:

- B1 (header): `status_line_text(snap, agent_name, attach_state=...)`
  (`interfaces/inline/textual_chat/chrome.py`) short-circuits BEFORE reading
  any `model`/`cost`/`ctx` field when `attach_state` is not `None` — a
  "connecting" or "failed" render can never be confused with a real value,
  by construction (there is no code path that reaches the placeholder
  `"—"`/`"$0.0000"` strings from a non-`None` attach_state).
- B2 (connecting vs failed): the two render VISIBLY DIFFERENT text, sourced
  from `AgentRegistry.attach_failed()` (new — `chat.py._background_attach`,
  #3671 P2, now calls `registry.record_background_attach_error(...)` on every
  failure path, not just logs it to a file the operator never sees).
- Decision 4B (composer): `on_composer_submitted` gates ordinary submission
  on `has_session()`, preserving the typed text and never touching
  `submit_user_text` / the #3300 sent-queue (there is no session to queue
  against yet) — `/quit`/`/exit` remain unblocked (an operator waiting on a
  slow attach must still be able to exit).

Real `AgentRegistry` (for the pure-registry tests) + a real, minimal
`ClientTransport` implementation (mirroring `ScriptedTransport` in
`test_textual_chat_phase4_3273.py`) + the real `TextualChatApp`/pilot — no
mocks, per the testing policy.
"""
from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat.chrome import Composer, status_line_text
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

# ---------------------------------------------------------------------------
# B1/B2 — status_line_text's own tri-state, pure-function level
# ---------------------------------------------------------------------------


def test_status_line_connecting_never_shows_a_model_or_cost_value():
    """Tier 2: B1 — a "connecting" state never reads model/cost/ctx off
    `snap`, even when `snap` is fully populated (a real attach could be about
    to overwrite it) — the two must never blend into a half-real line."""
    fully_populated_snap = {
        "model_active_class": "opus", "cost_agent": 1.2345, "attached_name": "alpha",
    }
    text = status_line_text(fully_populated_snap, "alpha", attach_state="connecting")
    assert "opus" not in text
    assert "1.2345" not in text
    assert "connecting" in text.lower()


def test_status_line_failed_is_visibly_different_from_connecting():
    """Tier 2: B2 — "connecting" and "failed" must not render identically;
    an operator glancing at the header must be able to tell them apart
    without reading a log file (owner ruling)."""
    connecting = status_line_text(None, "alpha", attach_state="connecting")
    failed = status_line_text(None, "alpha", attach_state="failed")
    assert connecting != failed
    assert "fail" in failed.lower()


def test_status_line_attached_state_unaffected():
    """Tier 2: `attach_state=None` (attached) is byte-identical to the
    pre-#3671-P3 behavior — no regression for the ordinary, already-attached
    case."""
    snap = {"model_active_class": "opus", "cost_agent": 0.5, "attached_name": "alpha"}
    assert status_line_text(snap, "alpha", attach_state=None) == status_line_text(snap, "alpha")


# ---------------------------------------------------------------------------
# B0's registry-side half — AgentRegistry.attach_failed()
# ---------------------------------------------------------------------------


def _registry(tmp_path, *, factory=None) -> AgentRegistry:
    if factory is None:
        def factory(profile: AgentProfile) -> Session:
            agent_dir = tmp_path / ".reyn" / "agents" / profile.name
            agent_dir.mkdir(parents=True, exist_ok=True)
            return make_session(
                agent_name=profile.name, agent_role=profile.role,
                output_language="en", snapshot_path=agent_dir / "state" / "snapshot.json",
            )
    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    return reg


def test_registry_attach_failed_false_before_any_recorded_failure(tmp_path):
    """Tier 2: a fresh registry (no attach attempted yet, or one in flight)
    reads `attach_failed() is False` — this is the "connecting" case, not
    "failed"."""
    reg = _registry(tmp_path)
    assert reg.attach_failed() is False


def test_registry_attach_failed_true_after_recorded_failure(tmp_path):
    """Tier 2: `record_background_attach_error` (called by
    `chat.py._background_attach` on every failure path) flips
    `attach_failed()` True — the exact seam `InProcessTransport.attach_failed`
    delegates to."""
    reg = _registry(tmp_path)
    reg.record_background_attach_error("boom")
    assert reg.attach_failed() is True


@pytest.mark.asyncio
async def test_registry_attach_failed_cleared_by_a_fresh_attach_attempt(tmp_path):
    """Tier 2: a NEW `attach()` call clears a stale prior failure up front —
    a retry must read as "connecting" again immediately, not "failed" until
    the retry also finishes (or fails again)."""
    reg = _registry(tmp_path)
    reg.record_background_attach_error("boom")
    assert reg.attach_failed() is True
    await reg.attach("alpha")
    assert reg.attach_failed() is False


# ---------------------------------------------------------------------------
# Decision 4B — composer blocks submission, preserves text, no #3300 touch
# ---------------------------------------------------------------------------


class _AttachStateTransport(ClientTransport):
    """A real, minimal `ClientTransport` (mirrors `ScriptedTransport` in
    `test_textual_chat_phase4_3273.py`) whose `has_session()`/
    `attach_failed()` are test-controlled, so the composer's gating can be
    driven through both states without a real registry/attach cycle."""

    def __init__(self) -> None:
        self._session = False
        self._failed = False
        self.submitted: list[str] = []
        self.displayed: list[OutboxMessage] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        for msg in self.displayed:
            yield DisplayFrame(msg)
        import asyncio
        await asyncio.Event().wait()

    async def submit_user_text(self, text: str) -> str:
        self.submitted.append(text)
        return "msg-1"

    async def answer_intervention_text(self, text: str, *, intervention_id=None) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str, *, intervention_id=None) -> bool:
        return False

    def has_session(self) -> bool:
        return self._session

    def attach_failed(self) -> bool:
        return self._failed

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        self.displayed.append(msg)

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


class _NoneReadModel(ChatReadModel):
    """Mirrors what `RegistryReadModel.snapshot()` genuinely returns
    pre-attach — `None` (see `status._snapshot`)."""

    @property
    def capabilities(self):
        # #4996: a test double simulating a fully-capable (local-shaped)
        # read model — every accessor above is a REAL, non-degraded
        # implementation for this test's own purposes, not a stand-in for
        # RemoteReadModel's frame-sufficiency boundary.
        return LOCAL_CHAT_READ_CAPABILITIES

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
        return True

    @property
    def history_path(self) -> Path:
        return Path("/tmp/reyn_p3_attach_state_history")

    def conversation_history(self, *, limit=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


@pytest.mark.asyncio
async def test_submit_blocked_while_unattached_preserves_typed_text():
    """Tier 2: decision 4B's core witness — typing while `has_session()` is
    `False` and pressing Enter must NOT clear the composer and must NOT call
    `submit_user_text` (the #3300 queue is never touched: nothing was ever
    submitted to it)."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    transport = _AttachStateTransport()
    app = TextualChatApp(transport=transport, read_model=_NoneReadModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.text = "hello, are you there"
        await pilot.pause()
        await app.on_composer_submitted(Composer.Submitted("hello, are you there"))
        await pilot.pause()

        assert transport.submitted == [], "must not reach the #3300 queue while unattached"
        assert composer.text == "hello, are you there", (
            "typed text must survive a blocked submission (decision 4B)"
        )
        # #5001: this notice is a client-authored one about THIS client's
        # own composer state — it now appends directly via `_ingest_frame`
        # rather than `transport.put_display` (a remote client's
        # `AgUiTransport.put_display` is a correct no-op; routing this
        # notice through it silently dropped it there). Assert on the
        # FlowView the operator actually sees, not the transport's own
        # `displayed` list, which this notice no longer reaches.
        rows = [e.item.text or "" for e in app.query_one(FlowView).entries]
        assert any("connecting" in t.lower() for t in rows), (
            "operator must be told WHY nothing happened"
        )


@pytest.mark.asyncio
async def test_submit_blocked_message_distinguishes_failed_from_connecting():
    """Tier 2: the SAME block, but with `attach_failed()` True — the notice
    text must say "failed", not "connecting" (B0 sourced from the same pair
    the header reads, so the two surfaces agree)."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    transport = _AttachStateTransport()
    transport._failed = True
    app = TextualChatApp(transport=transport, read_model=_NoneReadModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.text = "still there?"
        await pilot.pause()
        await app.on_composer_submitted(Composer.Submitted("still there?"))
        await pilot.pause()

        assert transport.submitted == []
        assert composer.text == "still there?"
        # #5001: same rerouting as above — see that test's own comment.
        rows = [e.item.text or "" for e in app.query_one(FlowView).entries]
        assert any("fail" in t.lower() for t in rows)


@pytest.mark.asyncio
async def test_submit_proceeds_normally_once_attached():
    """Tier 2: once `has_session()` flips True (attach completed), an
    ordinary submission clears the composer and reaches
    `submit_user_text` exactly as before #3671 P3 — no regression to the
    attached-path behavior."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    transport = _AttachStateTransport()
    transport._session = True
    app = TextualChatApp(transport=transport, read_model=_NoneReadModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.text = "go ahead"
        await pilot.pause()
        await app.on_composer_submitted(Composer.Submitted("go ahead"))
        await pilot.pause()

        assert transport.submitted == ["go ahead"]
        assert composer.text == ""


@pytest.mark.asyncio
async def test_quit_still_works_while_unattached():
    """Tier 2: an operator waiting on a slow/failed attach must still be able
    to exit — `/quit` is NOT gated behind `has_session()`."""
    from reyn.interfaces.inline.textual_chat import TextualChatApp

    transport = _AttachStateTransport()
    app = TextualChatApp(transport=transport, read_model=_NoneReadModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        await app.on_composer_submitted(Composer.Submitted("/quit"))
        await pilot.pause()
        assert not app.is_running


# ---------------------------------------------------------------------------
# Header end-to-end: the MOUNTED StatusLine widget reflects attach state
# ---------------------------------------------------------------------------
#
# Three separate app instances (one per state) rather than mutating one
# live — the header's live refresh is driven off frame arrival
# (`_refresh_live_chrome`, called from the frame-pump loop), and asserting
# through that would mean either reaching into `_status_text()` directly
# (a private-state assertion the tier policy forbids) or fabricating a fake
# live frame stream; a fresh app per state is simpler and stays entirely on
# the PUBLIC `StatusLine` widget surface, which is what actually renders to
# the operator.


@pytest.mark.asyncio
async def test_header_shows_connecting_state_at_mount():
    """Tier 2: B0/B1 — a never-attached transport renders the "connecting"
    header on the real, MOUNTED `StatusLine` widget."""
    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp

    transport = _AttachStateTransport()
    app = TextualChatApp(transport=transport, read_model=_NoneReadModel())
    async with app.run_test(size=(100, 30)):
        status = str(app.query_one(StatusLine).render())
        assert "connecting" in status.lower()


@pytest.mark.asyncio
async def test_header_shows_failed_state_at_mount():
    """Tier 2: B0/B2 — an `attach_failed()` transport renders the "failed"
    header, visibly different from "connecting", on the mounted widget."""
    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp

    transport = _AttachStateTransport()
    transport._failed = True
    app = TextualChatApp(transport=transport, read_model=_NoneReadModel())
    async with app.run_test(size=(100, 30)):
        status = str(app.query_one(StatusLine).render())
        assert "fail" in status.lower()


@pytest.mark.asyncio
async def test_header_shows_ordinary_state_once_attached():
    """Tier 2: B0 — an attached transport shows neither placeholder text,
    no regression for the ordinary case."""
    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp

    transport = _AttachStateTransport()
    transport._session = True
    app = TextualChatApp(transport=transport, read_model=_NoneReadModel())
    async with app.run_test(size=(100, 30)):
        status = str(app.query_one(StatusLine).render())
        assert "connecting" not in status.lower()
        assert "fail" not in status.lower()
