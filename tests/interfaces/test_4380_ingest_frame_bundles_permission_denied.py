"""Tier 2b: #4380 — ``TextualChatApp._ingest_frame`` bundles CONSECUTIVE
``permission_denied`` lifecycle markers into one row with a count, instead
of appending a new row per denial.

Owner ruling (#4380): of the 6 new markers this issue adds, only
``permission_denied`` bundles, and only its "same (kind, path, reason)"
repeats — never a different capability/path/reason, and never across an
intervening frame (compare against the IMMEDIATELY-PRECEDING row only,
the same conservative rule ``_coalesce_tool_result`` already established
for tool-call rows — mirrored here, not reinvented).

Driven through the REAL frame pump (a queue-backed ``ClientTransport`` +
a real mounted ``TextualChatApp``), not a direct ``_ingest_frame(...)``
call — the production path is ``transport.frames()`` -> ``_pump_frames``
-> ``_ingest_frame``, and this file exercises exactly that, the same
shape ``test_pipeline_single_flow_entry_3641.py``'s sibling app-level
tests and ``test_3310_n2_reset_hydrate.py``'s ``QueueTransport`` use.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.repl.read_model import ChatReadModel
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _NoHistoryReadModel(ChatReadModel):
    """A real, minimal :class:`ChatReadModel` — no restored history needed
    for these tests (every marker arrives as a LIVE frame)."""

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
    def history_path(self):
        from pathlib import Path
        return Path("/tmp/reyn_4380_input_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


class QueueTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` a test drives frame-by-frame
    (mirrors ``test_3310_n2_reset_hydrate.py``'s own helper of the same
    name/shape)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue" = asyncio.Queue()
        self.submitted: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        while True:
            yield await self._queue.get()

    def push_display(self, msg: OutboxMessage) -> None:
        self._queue.put_nowait(DisplayFrame(msg))

    async def submit_user_text(self, text: str) -> str:
        self.submitted.append(text)
        return ""

    async def answer_intervention_text(self, text: str, *, intervention_id=None) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str, *, intervention_id=None) -> bool:
        return True

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self):
        return None

    def put_display(self, msg: OutboxMessage) -> None:
        self.push_display(msg)

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def cancel_queued(self, msg_id: str) -> bool:  # pragma: no cover - trivial
        return False

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _denial_marker(kind: str, path: str, reason: str) -> OutboxMessage:
    """The exact shape ``lifecycle_forwarder.on_permission_denied`` emits —
    same text template, same ``lifecycle_bundle_key`` meta shape."""
    return OutboxMessage(
        kind="system",
        text=f"[✗ permission denied: {kind} {path}]",
        meta={"lifecycle_bundle_key": ("permission_denied", kind, path, reason)},
    )


def _texts(app: TextualChatApp) -> "list[str]":
    return [entry.item.text for entry in app.conversation]


@pytest.mark.asyncio
async def test_consecutive_identical_denials_bundle_into_one_row_with_a_count() -> None:
    """Tier 2b: 3 back-to-back same-(kind, path, reason) denials -> ONE row,
    ending in ``×3`` — not 3 separate rows."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, read_model=_NoHistoryReadModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for _ in range(3):
            transport.push_display(_denial_marker("file.write", "/etc/passwd", "denied by policy"))
            await pilot.pause()

        rows = _texts(app)
        assert rows == ["[✗ permission denied: file.write /etc/passwd] ×3"], (
            f"expected exactly one bundled row, got: {rows!r}"
        )


@pytest.mark.asyncio
async def test_a_denial_for_a_different_path_starts_its_own_row() -> None:
    """Tier 2b: owner's own constraint applied — same op kind, DIFFERENT
    path, must NOT bundle (a different capability denial is a different
    fact, not a repeat of the first)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, read_model=_NoHistoryReadModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        transport.push_display(_denial_marker("file.write", "/a", "denied by policy"))
        await pilot.pause()
        transport.push_display(_denial_marker("file.write", "/b", "denied by policy"))
        await pilot.pause()

        rows = _texts(app)
        assert rows == [
            "[✗ permission denied: file.write /a]",
            "[✗ permission denied: file.write /b]",
        ]


@pytest.mark.asyncio
async def test_a_denial_for_a_different_reason_starts_its_own_row() -> None:
    """Tier 2b: owner's own correction to the original (kind, path)
    proposal — same kind AND path, DIFFERENT reason, must NOT bundle."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, read_model=_NoHistoryReadModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        transport.push_display(_denial_marker("file.write", "/a", "denied by policy"))
        await pilot.pause()
        transport.push_display(_denial_marker("file.write", "/a", "sandbox violation"))
        await pilot.pause()

        rows = _texts(app)
        assert rows == [
            "[✗ permission denied: file.write /a]",
            "[✗ permission denied: file.write /a]",
        ]


@pytest.mark.asyncio
async def test_an_intervening_frame_breaks_the_bundle_chain() -> None:
    """Tier 2b: the most conservative rule — compare ONLY against the
    immediately-preceding row. Two identical denials with an unrelated
    frame between them must NOT bundle (owner: "something happened in
    between" is a real fact, not a defect to paper over)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, read_model=_NoHistoryReadModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        transport.push_display(_denial_marker("file.write", "/a", "denied by policy"))
        await pilot.pause()
        transport.push_display(OutboxMessage(kind="agent", text="unrelated reply"))
        await pilot.pause()
        transport.push_display(_denial_marker("file.write", "/a", "denied by policy"))
        await pilot.pause()

        rows = _texts(app)
        assert rows == [
            "[✗ permission denied: file.write /a]",
            "unrelated reply",
            "[✗ permission denied: file.write /a]",
        ], f"an intervening frame must reset the bundle chain, got: {rows!r}"


@pytest.mark.asyncio
async def test_the_audit_relevant_flowview_entry_count_shrinks_with_bundling() -> None:
    """Tier 2b: the DISPLAY collapses — proven directly against the real
    FlowView entry count (not just the text list), the same public surface
    ``test_pipeline_single_flow_entry_3641.py``'s sibling ONE-ROW claim is
    proven against elsewhere in this suite."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, read_model=_NoHistoryReadModel())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for _ in range(6):
            transport.push_display(_denial_marker("file.write", "/etc/passwd", "denied by policy"))
            await pilot.pause()

        entries = list(app.query_one(FlowView).entries)
        # Unpacking into exactly one binding is itself the "exactly one
        # entry" assertion — a wrong COUNT fails the unpack, not a bare
        # ``len(...) == N`` format pin (same idiom this file's sibling
        # producer-side test file uses for "warned exactly once").
        (only_entry,) = entries
        assert only_entry.item.text.endswith("×6")
