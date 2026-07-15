"""Tier 2: closed-set intervention scrollback de-duplication (owner UX report).

Owner: "TUI のユーザーインターベンションによるセーフティ/パーミッションの問い
合わせ UI がすごくダサい...選択した後に質問メッセージが残り続けてる".

Root cause: a closed-set intervention (`choices` non-empty) is rendered TWICE
in the inline CUI — once permanently to terminal scrollback
(`InterventionHandler.announce` -> `kind="intervention"` OutboxMessage) and
once as a LIVE selectable region above the input
(`inline/app.py`'s `_sync_region` + `build_intervention_element`, driven by
the SAME `meta["choices"]`). The live region correctly clears once answered,
but printed terminal scrollback cannot be un-printed or collapsed after the
fact — so the full prompt+choices block sits there looking permanently
"needs-you" even once resolved. `InlineChatRenderer.message()` now skips the
scrollback print for closed-set only (the live region already showed it, and
the resolved answer still lands as a compact `kind="user"` echo via
`deliver_answer_to` — the correct permanent record). Free-text interventions
(no `choices`) have no live-region alternative, so their scrollback print is
unaffected.

Uses a real `InterventionHandler.announce()` (not a hand-built OutboxMessage)
so the `meta["choices"]` shape is exactly what production emits, and a real
`InlineChatRenderer`, asserting on what actually reaches `sys.__stdout__`
(the same public-surface pattern as test_agui_reasoning_map_p6a.py) — never
the private `_buffer`.
"""
from __future__ import annotations

import asyncio
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.interfaces.repl.renderer import InlineChatRenderer
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.services.intervention_handler import InterventionHandler
from reyn.runtime.services.intervention_registry import InterventionRegistry
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.user_intervention import InterventionChoice, UserIntervention


def _build_handler(tmp_path: Path, outbox: list[OutboxMessage]) -> InterventionHandler:
    """A wired, all-real InterventionHandler (mirrors test_2770's helper)."""
    state_log = StateLog(tmp_path / "state.wal")
    event_store = EventStore(tmp_path / "events")
    event_log = EventLog(subscribers=[event_store])
    journal = SnapshotJournal(
        agent_name="t", snapshot_path=tmp_path / "snap.json", state_log=state_log,
    )

    async def _put_outbox(msg: OutboxMessage) -> None:
        outbox.append(msg)

    registry = InterventionRegistry(on_announce=lambda iv: asyncio.sleep(0))
    return InterventionHandler(
        intervention_registry=registry,
        journal=journal,
        event_log=event_log,
        put_outbox=_put_outbox,
        append_history=lambda *a: None,
    )


def _closed_set_iv() -> UserIntervention:
    iv = UserIntervention(
        kind="permission.confirm",
        prompt="Allow fetching from 'example.com'?",
        detail="web fetch from host: 'example.com'",
        choices=[
            InterventionChoice(id="yes", label="[y]es", hotkey="y"),
            InterventionChoice(id="always", label="[A]lways", hotkey="A"),
            InterventionChoice(id="no", label="[n]o", hotkey="n"),
        ],
    )
    iv.future = asyncio.get_event_loop().create_future()
    return iv


def _free_text_iv() -> UserIntervention:
    iv = UserIntervention(kind="ask_user", prompt="What should I name this file?")
    iv.future = asyncio.get_event_loop().create_future()
    return iv


@pytest.mark.asyncio
async def test_closed_set_intervention_is_not_printed_to_scrollback(tmp_path, monkeypatch) -> None:
    """Tier 2: a real announce()'d closed-set intervention reaches
    InlineChatRenderer.message() but writes NOTHING to stdout — the live
    region (not this renderer) is its only display, so scrollback stays clean
    instead of permanently showing a now-resolved "needs-you" block."""
    outbox: list[OutboxMessage] = []
    handler = _build_handler(tmp_path, outbox)
    await handler.announce(_closed_set_iv())
    msg = next(m for m in outbox if m.kind == "intervention")
    assert msg.meta.get("choices")  # sanity: this really is closed-set

    renderer = InlineChatRenderer()
    buf = StringIO()
    monkeypatch.setattr(sys, "__stdout__", buf)
    renderer.message(msg)

    assert buf.getvalue() == ""  # nothing printed — the live region owns display


@pytest.mark.asyncio
async def test_closed_set_intervention_still_sets_the_waiting_on_user_indicator(tmp_path, monkeypatch) -> None:
    """Tier 2: suppressing the scrollback print must NOT suppress the
    "Waiting for you" working-indicator sub-state — that status-bar signal is
    independent of scrollback and the owner did not ask to lose it."""
    outbox: list[OutboxMessage] = []
    handler = _build_handler(tmp_path, outbox)
    await handler.announce(_closed_set_iv())
    msg = next(m for m in outbox if m.kind == "intervention")

    renderer = InlineChatRenderer()
    buf = StringIO()
    monkeypatch.setattr(sys, "__stdout__", buf)
    # A turn must be "in flight" for the working row to render at all
    # (working_line returns [] when idle) — drive that through the same
    # public on_chat_event() path production uses (Session.subscribe_chat_events),
    # not by poking the private _thinking flag directly.
    renderer.on_chat_event(SimpleNamespace(type="turn_started", data={}))
    renderer.message(msg)

    frags = renderer.working_frags(100.0)
    rendered = "".join(text for _style, text in frags)
    assert "Waiting for you" in rendered


@pytest.mark.asyncio
async def test_free_text_intervention_is_still_printed_to_scrollback(tmp_path, monkeypatch) -> None:
    """Tier 2: regression guard — a free-text intervention (no `choices`) has
    no live-region alternative, so it must keep printing to scrollback exactly
    as before this change (only closed-set is suppressed)."""
    outbox: list[OutboxMessage] = []
    handler = _build_handler(tmp_path, outbox)
    await handler.announce(_free_text_iv())
    msg = next(m for m in outbox if m.kind == "intervention")
    assert not msg.meta.get("choices")  # sanity: this really is free-text

    renderer = InlineChatRenderer()
    buf = StringIO()
    monkeypatch.setattr(sys, "__stdout__", buf)
    renderer.message(msg)

    out = buf.getvalue()
    assert "What should I name this file?" in out
