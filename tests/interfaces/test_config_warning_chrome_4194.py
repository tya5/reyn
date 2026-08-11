"""Tier 2: #4194 — the bottom-chrome config-warning indicator.

Owner condition: the interactive CUI must show SOMETHING when a
``reyn.yaml``/``reyn.local.yaml``/``~/.reyn/config.yaml`` key was not
applied — architect's live measurement found the existing
``_warn_unknown_config_keys`` warning only ever reached a log file
(``_setup_interactive_logging`` redirects all logs there), invisible to an
operator running the interactive CUI. Architect's design ruling fixes three
properties, form left to the implementer: ①doesn't scroll away with the
conversation ②stays visible for as long as the condition holds ③directs the
operator to ``reyn config validate`` for detail.

No mocks of collaborators: a real ``ReynConfig`` (cheaply constructible —
just ``ReynConfig(unknown_config_key_count=N)``), a real ``TextualChatApp``
run headless via ``App.run_test`` (the same harness
``test_conversation_chrome_separation.py`` already uses for chrome-geometry
assertions).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.config import ReynConfig
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import (
    ConfigWarningLine,
    config_warning_text,
)
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import EventFrame
from reyn.schemas.models import Event


class _Transport(ClientTransport):
    """Minimal real ClientTransport implementing the CURRENT full abstract
    surface (test_conversation_chrome_separation.py's own stub predates
    #3903/#4166's cancel_inflight()/has_session()/etc additions and would
    no longer instantiate — this one is up to date)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> str:  # pragma: no cover
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
        return False

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg) -> None:  # pragma: no cover - trivial
        pass

    async def cancel_inflight(self) -> str:
        return "nothing was running"

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


# ─── 1. config_warning_text — pure function ──────────────────────────────


def test_config_warning_text_none_when_zero():
    """Tier 2: a clean config (no unknown keys) shows nothing — the
    indicator does not occupy a row for a no-op condition."""
    assert config_warning_text(0) is None


def test_config_warning_text_singular_and_plural():
    """Tier 2: the count is legible English — "1 config key" not "1 config
    keys" — and always names the fix command by name, matching architect's
    ③ requirement (directs to `reyn config validate`)."""
    assert config_warning_text(1) == "⚠ 1 config key not applied → reyn config validate"
    assert config_warning_text(3) == "⚠ 3 config keys not applied → reyn config validate"


# ─── 2. Real headless app — the indicator actually mounts (or doesn't) ───


@pytest.mark.asyncio
async def test_indicator_absent_on_a_clean_config():
    """Tier 2b: accept-side sibling — a config with 0 unknown keys mounts
    NO ConfigWarningLine at all (not merely hidden — see the widget's own
    docstring for why absence, not hiding, is the design). Without this
    test, an implementation that always shows a "0 keys" row would still
    pass every other test in this file."""
    config = ReynConfig(unknown_config_key_count=0)
    app = TextualChatApp(transport=_Transport(), config=config)
    async with app.run_test(size=(90, 24)):
        assert len(app.query("ConfigWarningLine")) == 0


@pytest.mark.asyncio
async def test_indicator_mounts_and_shows_the_count():
    """Tier 2: the owner's literal condition — a config with unknown keys
    shows something in the interactive CUI, not just a log line. Positive
    witness: the real mounted widget's text names the actual count."""
    config = ReynConfig(unknown_config_key_count=2)
    app = TextualChatApp(transport=_Transport(), config=config)
    async with app.run_test(size=(90, 24)):
        widget = app.query_one(ConfigWarningLine)
        assert "2 config keys" in str(widget.content)
        assert "reyn config validate" in str(widget.content)


@pytest.mark.asyncio
async def test_indicator_does_not_scroll_away_or_clip_the_conversation():
    """Tier 2: architect's ①doesn't-scroll-away + the #4194 geometry
    measurement's own safety claim — mounting the indicator does not
    overlap or clip FlowView (the conversation pane), it simply takes one
    row that FlowView's `1fr` sizing absorbs. Appending messages (so
    FlowView actually has scrollable content) and re-checking the
    indicator is still mounted and at a fixed, non-conversation-following
    position is the closest a headless test gets to "does not scroll
    away" — a row inside the conversation's own scroll region would move
    with it; this one is a sibling outside that region."""
    from textual_flowview import FlowView

    from reyn.interfaces.inline.textual_chat import Composer
    from reyn.runtime.outbox import OutboxMessage

    config = ReynConfig(unknown_config_key_count=1)
    app = TextualChatApp(transport=_Transport(), config=config)
    async with app.run_test(size=(90, 24)) as pilot:
        app.query_one(Composer).focus()
        await pilot.pause()
        indicator = app.query_one(ConfigWarningLine)
        y_before = indicator.region.y
        for i in range(6):
            app.conversation.append(OutboxMessage(kind="agent", text=f"reply {i}"))
        await pilot.pause()
        flow = app.query_one(FlowView)
        # No overlap: the indicator's row is entirely above or below FlowView's
        # region, never inside its y-range.
        flow_top, flow_bottom = flow.region.y, flow.region.y + flow.region.height
        assert not (flow_top <= indicator.region.y < flow_bottom), (
            f"indicator at y={indicator.region.y} overlaps FlowView "
            f"[{flow_top}, {flow_bottom})"
        )
        # Still mounted at a stable position — did not get pushed off-screen
        # or removed by the conversation growing.
        assert app.query_one(ConfigWarningLine).region.y == y_before


@pytest.mark.asyncio
async def test_indicator_absent_when_config_is_none():
    """Tier 2b: accept-side sibling for the pre-session/no-config shape —
    ``TextualChatApp(config=None)`` (the default every other app test in
    this package already constructs with) must not raise and must not
    mount the indicator. Real production shape: a remote client or an
    early construction window before config is threaded through."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(90, 24)):
        assert len(app.query("ConfigWarningLine")) == 0
