"""Tier 2: destructive slash commands require 2-step confirm.

/pending discard mirrors /reset's pattern:
  - First invocation (no "confirm" suffix) → warning + hint; action NOT taken.
  - Second invocation (same args + " confirm") → action proceeds.

This prevents a misclick on a Tab-completed prefix from immediately
discarding an intervention.

Pinned per task spec:
  1. /pending discard <id> (no confirm) → warning; API NOT called.
  2. /pending discard <id> confirm → API called.
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

from reyn.interfaces.slash import REGISTRY
from reyn.runtime.outbox import OutboxMessage


def _get_cmd(name: str):
    cmd = REGISTRY.get(name)
    assert cmd is not None, f"/{name} must be registered"
    return cmd


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
