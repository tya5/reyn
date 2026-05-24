"""Tier 2: destructive slash commands require 2-step confirm (Wave-13 B#2).

/cancel, /plan discard, and /pending discard now mirror /reset's pattern:
  - First invocation (no "confirm" suffix) → warning + hint; action NOT taken.
  - Second invocation (same args + " confirm") → action proceeds.

This prevents a misclick on a Tab-completed prefix from immediately
aborting a skill, plan, or intervention.

Pinned per task spec:
  1. /cancel <id> (no confirm) → outbox carries warning + "confirm" hint;
     task.cancel() NOT called.
  2. /cancel <id> confirm → task.cancel() called.
  3. /plan discard <id> (no confirm) → warning; plan NOT discarded.
  4. /plan discard <id> confirm → plan IS discarded.
  5. /pending discard <id> (no confirm) → warning; API NOT called.
  6. /pending discard <id> confirm → API called.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import REGISTRY

# ── /cancel stubs ─────────────────────────────────────────────────────────


class _CancelTask:
    """Minimal asyncio.Task stand-in that records cancel() calls."""

    def __init__(self):
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        """Public read: True after cancel() has been called."""
        return self._cancelled

    def cancel(self):
        self._cancelled = True

    def done(self):
        return False


class _CancelSession:
    """Stub session for /cancel tests."""

    def __init__(self, rid: str):
        self._rid = rid
        self._task = _CancelTask()
        self.running_skills = {rid: self._task}
        self.running_skills_started_at = {}
        self.outbox_messages: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox_messages.append(msg)

    def _resolve_run_id(self, prefix: str):
        """Return (rid, []) when prefix matches, else (None, [])."""
        matches = [r for r in self.running_skills if r.startswith(prefix)]
        if len(matches) == 1:
            return matches[0], []
        if len(matches) > 1:
            return None, matches
        return None, []


def _get_cmd(name: str):
    cmd = REGISTRY.get(name)
    assert cmd is not None, f"/{name} must be registered"
    return cmd


# ── Test 1: /cancel <id> (no confirm) → warning; task NOT cancelled ───────


@pytest.mark.asyncio
async def test_cancel_no_confirm_shows_warning_not_cancelled() -> None:
    """Tier 2: /cancel <id> without confirm emits warning; task.cancel() NOT called."""
    rid = "20250101_my_skill_abcd"
    sess = _CancelSession(rid)
    cmd = _get_cmd("cancel")
    await cmd.handler(sess, rid)

    # task must NOT be cancelled
    assert not sess._task.cancelled

    # outbox must contain a system warning with "confirm" hint
    warn_msgs = [m for m in sess.outbox_messages if m.kind == "system"]
    assert warn_msgs, (
        f"expected at least one warning system message, got none: "
        f"{[m.text for m in sess.outbox_messages]}"
    )
    assert "confirm" in warn_msgs[0].text
    assert rid in warn_msgs[0].text or "abcd" in warn_msgs[0].text


# ── Test 2: /cancel <id> confirm → task.cancel() called ──────────────────


@pytest.mark.asyncio
async def test_cancel_with_confirm_cancels_task() -> None:
    """Tier 2: /cancel <id> confirm calls task.cancel()."""
    rid = "20250101_my_skill_abcd"
    sess = _CancelSession(rid)
    cmd = _get_cmd("cancel")
    await cmd.handler(sess, f"{rid} confirm")

    assert sess._task.cancelled

    # outbox must carry the cancel-requested system message
    system_msgs = [m for m in sess.outbox_messages if m.kind == "system"]
    assert any("cancel" in m.text.lower() for m in system_msgs), (
        f"expected 'cancel' in system outbox, got: {[m.text for m in system_msgs]}"
    )


# ── /plan discard stubs and tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_discard_no_confirm_shows_warning(tmp_path, monkeypatch):
    """Tier 2: /plan discard <id> (no confirm) → warning msg; plan not discarded."""
    monkeypatch.chdir(tmp_path)
    from reyn.chat.session import ChatSession
    from reyn.events.state_log import StateLog

    session = ChatSession(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )
    session.is_attached = True

    await session._journal.record_plan_started(
        plan_id="p_warn", goal="g", n_steps=2,
    )
    assert "p_warn" in session._journal.snapshot.active_plan_ids

    await session._maybe_handle_slash("/plan discard p_warn")

    # Plan must still be active.
    assert "p_warn" in session._journal.snapshot.active_plan_ids

    # Outbox must carry a warning with "confirm" hint.
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    warn_msgs = [m for m in msgs if m.kind == "system"]
    assert warn_msgs, f"expected system warning, got: {[(m.kind, m.text) for m in msgs]}"
    assert "confirm" in warn_msgs[0].text
    assert "p_warn" in warn_msgs[0].text


@pytest.mark.asyncio
async def test_plan_discard_with_confirm_discards_plan(tmp_path, monkeypatch):
    """Tier 2: /plan discard <id> confirm discards the plan (clears active_plan_ids)."""
    monkeypatch.chdir(tmp_path)
    from reyn.chat.session import ChatSession
    from reyn.events.state_log import StateLog

    session = ChatSession(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )
    session.is_attached = True

    await session._journal.record_plan_started(
        plan_id="p_confirm", goal="g", n_steps=2,
    )

    consumed = await session._maybe_handle_slash(
        "/plan discard p_confirm confirm",
    )
    assert consumed is True

    # Plan must be cleared.
    assert "p_confirm" not in session._journal.snapshot.active_plan_ids

    # Outbox must carry the "discarded plan run" confirmation.
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    status_texts = [m.text for m in msgs if m.kind == "system"]
    assert any("discarded plan run" in t for t in status_texts), (
        f"expected 'discarded plan run' in system msgs, got: {status_texts}"
    )


# ── /pending discard stubs and tests ─────────────────────────────────────


@dataclass
class _PendingOpStub:
    id: str
    kind: str
    origin_channel_id: str
    created_at: str = ""
    summary: str = ""
    detail: str = ""


class _PendingStubSession:
    """Minimal session stub for /pending dispatch tests."""

    def __init__(
        self,
        *,
        pending_ops: list | None = None,
        agent_name: str = "default",
        discard_result: bool = True,
    ) -> None:
        self._pending = pending_ops or []
        self.agent_name = agent_name
        self._discard_result = discard_result
        self.outbox_messages: list[OutboxMessage] = []
        self.discard_calls: list[str] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox_messages.append(msg)

    def list_stalled_interventions(self) -> list:
        return list(self._pending)

    async def discard_pending_intervention(
        self, iv_id: str, *, reason: str = "user_discarded",
    ) -> bool:
        self.discard_calls.append(iv_id)
        return self._discard_result


@pytest.mark.asyncio
async def test_pending_discard_no_confirm_shows_warning_not_discarded() -> None:
    """Tier 2: /pending discard <id> (no confirm) → warning; API NOT called."""
    sess = _PendingStubSession(pending_ops=[
        _PendingOpStub(
            id="iv-abcd1234", kind="ask_user",
            origin_channel_id="tui:x", summary="Allow exec?",
        ),
    ])
    cmd = _get_cmd("pending")
    await cmd.handler(sess, "discard iv-abcd1234")

    # API must NOT be called.
    assert sess.discard_calls == []

    # Warning with "confirm" hint must be in the outbox.
    warn_msgs = [m for m in sess.outbox_messages if m.kind == "system"]
    assert warn_msgs, (
        f"expected at least one system warning, got none: "
        f"{[m.text for m in sess.outbox_messages]}"
    )
    assert "confirm" in warn_msgs[0].text


@pytest.mark.asyncio
async def test_pending_discard_with_confirm_calls_api() -> None:
    """Tier 2: /pending discard <id> confirm calls discard_pending_intervention."""
    sess = _PendingStubSession(pending_ops=[
        _PendingOpStub(
            id="iv-abcd1234", kind="ask_user",
            origin_channel_id="tui:x", summary="Allow exec?",
        ),
    ])
    cmd = _get_cmd("pending")
    await cmd.handler(sess, "discard iv-abcd1234 confirm")

    assert sess.discard_calls == ["iv-abcd1234"]
    reply_msgs = [m for m in sess.outbox_messages if m.kind == "system"]
    assert any("discarded" in m.text for m in reply_msgs)
