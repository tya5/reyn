"""Tier 2b: /pending slash command — list / discard / claim dispatch.

Issue #277 — Layer 3 of the TUI surface bundle. Pins the slash
command's argument parsing + dispatch to the session-level
``list_stalled_interventions`` / ``discard_pending_intervention`` /
``claim_pending_intervention`` APIs introduced by PR #275.

Drives a real REGISTRY (= no MagicMock per testing.ja.md) with a
stub session that records the API calls made.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.slash import REGISTRY
from reyn.runtime.outbox import OutboxMessage


@dataclass
class _PendingOpStub:
    id: str
    kind: str
    origin_channel_id: str
    created_at: str = ""
    summary: str = ""
    detail: str = ""


class _StubSession:
    """Minimal session-shaped recorder for `/pending` dispatch tests.

    Records every API call the slash handler makes against the
    session, returning configurable stubbed results. Uses real
    ``OutboxMessage`` for the outbox capture (= no MagicMock).
    """

    def __init__(
        self,
        *,
        pending_ops: list | None = None,
        agent_name: str = "default",
        discard_result: bool = True,
        claim_result: _PendingOpStub | None = None,
    ) -> None:
        self._pending = pending_ops or []
        self.agent_name = agent_name
        self._discard_result = discard_result
        self._claim_result = claim_result
        self.outbox_messages: list[OutboxMessage] = []
        self.discard_calls: list[str] = []
        self.claim_calls: list[tuple[str, str]] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox_messages.append(msg)

    def list_stalled_interventions(self) -> list:
        return list(self._pending)

    async def discard_pending_intervention(
        self, iv_id: str, *, reason: str = "user_discarded",
    ) -> bool:
        self.discard_calls.append(iv_id)
        return self._discard_result

    async def claim_pending_intervention(
        self, iv_id: str, new_channel_id: str,
    ):
        self.claim_calls.append((iv_id, new_channel_id))
        return self._claim_result


def _get_pending_cmd():
    cmd = REGISTRY.get("pending")
    assert cmd is not None, "/pending slash must be registered"
    return cmd


@pytest.mark.asyncio
async def test_pending_slash_is_registered() -> None:
    """Tier 2b: /pending appears in the slash registry with a summary."""
    cmd = _get_pending_cmd()
    assert cmd.name == "pending"
    assert cmd.summary  # non-empty summary so /help renders it


@pytest.mark.asyncio
async def test_pending_list_renders_stalled_ops() -> None:
    """Tier 2b: ``/pending`` (= alias for list) emits a reply containing each op."""
    sess = _StubSession(pending_ops=[
        _PendingOpStub(
            id="iv-abcd1234", kind="intervention",
            origin_channel_id="tui:planner", summary="Allow exec?",
        ),
    ])
    cmd = _get_pending_cmd()
    await cmd.handler(sess, "")
    # At least one outbox reply produced (kind=system) containing the iv info.
    reply_msgs = [m for m in sess.outbox_messages if m.kind == "system"]
    assert reply_msgs, "expected at least one system reply"
    text = reply_msgs[0].text
    assert "intervention" in text
    assert "iv-abcd1" in text  # short-id form (first 8 chars)
    assert "tui:planner" in text
    assert "Allow exec?" in text


@pytest.mark.asyncio
async def test_pending_list_empty_returns_friendly_text() -> None:
    """Tier 2b: empty stalled list → "no pending operations" reply."""
    sess = _StubSession(pending_ops=[])
    cmd = _get_pending_cmd()
    await cmd.handler(sess, "list")
    reply_msgs = [m for m in sess.outbox_messages if m.kind == "system"]
    assert reply_msgs, "expected at least one system reply"
    assert "no pending" in reply_msgs[0].text.lower()


@pytest.mark.asyncio
async def test_pending_discard_first_invocation_shows_warning() -> None:
    """Tier 2b: ``/pending discard <id>`` (no confirm) emits a warning and
    does NOT call discard_pending_intervention (Wave-13 B#2 confirm parity)."""
    sess = _StubSession(pending_ops=[
        _PendingOpStub(
            id="iv-abcd1234", kind="intervention",
            origin_channel_id="tui:x", summary="ok?",
        ),
    ])
    cmd = _get_pending_cmd()
    await cmd.handler(sess, "discard iv-abcd1234")
    # Must NOT have called the API.
    assert sess.discard_calls == []
    # Must emit a warning with "confirm" hint.
    reply_msgs = [m for m in sess.outbox_messages if m.kind == "system"]
    assert reply_msgs, "expected at least one system reply"
    assert "confirm" in reply_msgs[0].text


@pytest.mark.asyncio
async def test_pending_discard_invokes_session_api_with_confirm() -> None:
    """Tier 2b: ``/pending discard <id> confirm`` calls
    ``discard_pending_intervention`` (2-step confirm suffix)."""
    sess = _StubSession(pending_ops=[
        _PendingOpStub(
            id="iv-abcd1234", kind="intervention",
            origin_channel_id="tui:x", summary="ok?",
        ),
    ])
    cmd = _get_pending_cmd()
    await cmd.handler(sess, "discard iv-abcd1234 confirm")
    assert sess.discard_calls == ["iv-abcd1234"]
    reply_msgs = [m for m in sess.outbox_messages if m.kind == "system"]
    assert any("discarded" in m.text for m in reply_msgs)


@pytest.mark.asyncio
async def test_pending_discard_resolves_short_prefix_id() -> None:
    """Tier 2b: ``discard confirm`` accepts a short prefix that uniquely
    matches one iv.

    Mirrors the Pending tab's UX (= ``id[:8]`` short form is what the
    user sees in the list output).  The confirm suffix must be stripped
    before prefix resolution.
    """
    sess = _StubSession(pending_ops=[
        _PendingOpStub(
            id="iv-abcd1234", kind="intervention",
            origin_channel_id="tui:x", summary="ok?",
        ),
    ])
    cmd = _get_pending_cmd()
    await cmd.handler(sess, "discard iv-abcd1 confirm")
    assert sess.discard_calls == ["iv-abcd1234"]


@pytest.mark.asyncio
async def test_pending_claim_invokes_session_api_with_tui_channel() -> None:
    """Tier 2b: ``/pending claim <id>`` calls ``claim_pending_intervention``
    with ``new_channel_id="tui:<agent>"``."""
    sess = _StubSession(
        pending_ops=[
            _PendingOpStub(
                id="iv-abcd1234", kind="intervention",
                origin_channel_id="a2a:peer", summary="claim me",
            ),
        ],
        agent_name="research",
        claim_result=_PendingOpStub(
            id="iv-abcd1234", kind="intervention",
            origin_channel_id="tui:research", summary="claim me",
        ),
    )
    cmd = _get_pending_cmd()
    await cmd.handler(sess, "claim iv-abcd1234")
    assert sess.claim_calls == [("iv-abcd1234", "tui:research")]
    reply_msgs = [m for m in sess.outbox_messages if m.kind == "system"]
    assert any("claimed" in m.text for m in reply_msgs)


@pytest.mark.asyncio
async def test_pending_discard_unknown_id_emits_error() -> None:
    """Tier 2b: discard with unknown id emits an error reply (no API call)."""
    sess = _StubSession(pending_ops=[
        _PendingOpStub(
            id="iv-abcd1234", kind="intervention",
            origin_channel_id="tui:x", summary="",
        ),
    ])
    cmd = _get_pending_cmd()
    await cmd.handler(sess, "discard iv-nonexistent")
    assert sess.discard_calls == []
    error_msgs = [m for m in sess.outbox_messages if m.kind == "error"]
    assert error_msgs


@pytest.mark.asyncio
async def test_pending_unknown_subcommand_emits_usage_error() -> None:
    """Tier 2b: ``/pending bogus`` → usage error reply."""
    sess = _StubSession(pending_ops=[])
    cmd = _get_pending_cmd()
    await cmd.handler(sess, "bogus")
    error_msgs = [m for m in sess.outbox_messages if m.kind == "error"]
    assert error_msgs
    assert "Usage" in error_msgs[0].text or "usage" in error_msgs[0].text
