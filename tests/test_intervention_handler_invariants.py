"""Tier 2: OS invariant tests for InterventionHandler.

Tests the extracted InterventionHandler service class
(FP-0019 Wave 2 part 1) in isolation using real instances — no mocks.

Invariants exercised:
  1. dispatch() emits ``user_intervention_requested`` (via WAL
     intervention_dispatched) before the future blocks — P6 audit trail.
  2. maybe_answer() returns False when no intervention is pending.
  3. deliver_answer_to() returns the resolved answer when a matching
     answer is provided — the wait completes.

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- No unittest.mock / AsyncMock / patch usage.
- Real InterventionHandler + InterventionRegistry + SnapshotJournal
  instances, wired with in-process callbacks.
- Public surface observed: WAL events via StateLog.iter_from(),
  asyncio Future resolution, outbox queue.
- Each test docstring starts with its Tier.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.chat.outbox import OutboxMessage
from reyn.chat.services.intervention_handler import InterventionHandler
from reyn.chat.services.intervention_registry import InterventionRegistry
from reyn.chat.services.snapshot_journal import SnapshotJournal
from reyn.events.event_store import EventStore
from reyn.events.events import EventLog
from reyn.events.state_log import StateLog
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionChoice,
    UserIntervention,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_handler(
    tmp_path: Path,
    *,
    outbox_items: list[OutboxMessage] | None = None,
    history_items: list[dict] | None = None,
) -> tuple[InterventionHandler, InterventionRegistry]:
    """Build a wired InterventionHandler + InterventionRegistry pair.

    All I/O is redirected to ``tmp_path``.  Collected outbox messages and
    history entries are appended to the provided lists (or discarded when
    None).

    Returns ``(handler, registry)`` so tests can dispatch to the registry
    directly when needed.
    """
    if outbox_items is None:
        outbox_items = []
    if history_items is None:
        history_items = []

    state_log = StateLog(tmp_path / "state.wal")
    event_store = EventStore(tmp_path / "events")
    event_log = EventLog(subscribers=[event_store])

    snapshot_path = tmp_path / "snap.json"
    journal = SnapshotJournal(
        agent_name="test_agent",
        snapshot_path=snapshot_path,
        state_log=state_log,
    )

    # Registry is wired with the handler's announce later; we construct
    # a placeholder first and patch after.
    async def _put_outbox(msg: OutboxMessage) -> None:
        outbox_items.append(msg)

    def _append_history(role: str, text: str, ts: str, meta: dict) -> None:
        history_items.append({"role": role, "text": text, "ts": ts, "meta": meta})

    # Registry needs on_announce; use a lambda that will call back into
    # the handler.  We construct the handler first, then build the registry
    # with the handler's announce as on_announce.  To avoid a chicken-and-egg
    # problem, we use a list to hold the handler reference.
    handler_ref: list[InterventionHandler] = []

    async def _on_announce(iv: UserIntervention) -> None:
        if handler_ref:
            await handler_ref[0].announce(iv)

    registry = InterventionRegistry(on_announce=_on_announce)

    handler = InterventionHandler(
        intervention_registry=registry,
        journal=journal,
        event_log=event_log,
        put_outbox=_put_outbox,
        append_history=_append_history,
    )
    handler_ref.append(handler)
    return handler, registry


def _make_iv(
    *,
    run_id: str | None = "run-001",
    prompt: str = "What's your name?",
    choices: list[InterventionChoice] | None = None,
    kind: str = "ask_user",
) -> UserIntervention:
    iv = UserIntervention(kind=kind, prompt=prompt, run_id=run_id, choices=choices or [])
    iv.future = asyncio.get_running_loop().create_future()
    return iv


def _wal_events(tmp_path: Path) -> list[dict]:
    log = StateLog(tmp_path / "state.wal")
    return list(log.iter_from(0))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_emits_intervention_requested_event(tmp_path, monkeypatch):
    """Tier 2: dispatch() writes ``intervention_dispatched`` to WAL before blocking.

    P6 invariant: the WAL must carry the dispatch event while the
    coroutine is awaiting the user's answer so a crash mid-await
    leaves an auditable trace for crash-recovery.
    """
    monkeypatch.chdir(tmp_path)
    handler, registry = _build_handler(tmp_path)

    iv = _make_iv(run_id="rX", prompt="Continue?")

    # Start dispatch in background; it will block on iv.future.
    task = asyncio.ensure_future(handler.dispatch(iv))

    # Yield twice so the coroutine runs up to the `await registry.dispatch(iv)`
    # line (which internally awaits iv.future).
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # At this point the future is still pending — read the WAL.
    events = _wal_events(tmp_path)
    dispatched = [e for e in events if e["kind"] == "intervention_dispatched"]
    assert len(dispatched) == 1, (
        f"expected 1 intervention_dispatched before future resolves; "
        f"got {[e['kind'] for e in events]}"
    )
    ev = dispatched[0]
    assert ev["intervention_id"] == iv.id
    assert ev["iv_dict"]["prompt"] == "Continue?"
    assert ev["iv_dict"]["run_id"] == "rX"
    assert "future" not in ev["iv_dict"]

    # Clean up: resolve the future and await the task.
    iv.future.set_result(InterventionAnswer(text="yes"))
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_maybe_answer_returns_false_when_no_pending(tmp_path, monkeypatch):
    """Tier 2: maybe_answer() is a no-op when no intervention is queued.

    Calling maybe_answer with text when the registry is empty must
    return False so the caller (session._handle_user_message) continues
    to route the text as a fresh router turn rather than silently
    consuming it.
    """
    monkeypatch.chdir(tmp_path)
    handler, _registry = _build_handler(tmp_path)

    result = await handler.maybe_answer("hello")
    assert result is False, (
        "maybe_answer must return False when no intervention is pending"
    )


@pytest.mark.asyncio
async def test_wait_for_answer_returns_intervention_answer(tmp_path, monkeypatch):
    """Tier 2: dispatch() completes and returns the answer when text is delivered.

    Verifies the round-trip: dispatch blocks → answer delivered via
    maybe_answer → dispatch returns the InterventionAnswer.

    This is the session-layer equivalent of
    ``_wait_for_intervention_answer`` as described in the FP-0019 spec.
    """
    monkeypatch.chdir(tmp_path)
    outbox: list[OutboxMessage] = []
    history: list[dict] = []
    handler, _registry = _build_handler(tmp_path, outbox_items=outbox, history_items=history)

    iv = _make_iv(run_id="rY", prompt="What city?")

    # Dispatch in background.
    dispatch_task: asyncio.Task[InterventionAnswer] = asyncio.ensure_future(
        handler.dispatch(iv)
    )

    # Let the dispatch coroutine reach its await point.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Deliver answer via maybe_answer (the user's text-input path).
    consumed = await handler.maybe_answer("Tokyo")
    assert consumed is True, "maybe_answer must consume the text when iv is pending"

    # Collect the dispatch result.
    answer = await asyncio.gather(dispatch_task, return_exceptions=True)
    answer = answer[0]

    assert isinstance(answer, InterventionAnswer), (
        f"dispatch must return InterventionAnswer; got {answer!r}"
    )
    assert answer.text == "Tokyo"

    # Verify history entry was appended.
    assert len(history) == 1
    assert history[0]["role"] == "user"
    assert history[0]["text"] == "Tokyo"
    assert history[0]["meta"]["intervention_id"] == iv.id

    # Verify outbox: intervention announcement + intervention_resolved.
    outbox_kinds = [m.kind for m in outbox]
    assert "intervention" in outbox_kinds, (
        "announce must put an 'intervention' message in outbox"
    )
    assert "intervention_resolved" in outbox_kinds, (
        "successful answer must put 'intervention_resolved' in outbox"
    )

    # Verify WAL has both dispatched and resolved events.
    events = _wal_events(tmp_path)
    kinds = [e["kind"] for e in events]
    assert "intervention_dispatched" in kinds
    assert "intervention_resolved" in kinds
