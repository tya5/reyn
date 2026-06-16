"""Tier 2: intervention announce surfaces prompt + detail structurally (issue #163).

Pre-fix, ``intervention_handler.announce`` concatenated ``prompt +
detail + choices_labels`` into one ``msg.text`` string, and the TUI
widget rendered the whole blob as a single bold-amber Label. Detail
(e.g. ``web fetch: <url>``) and the choices hint line were visually
indistinguishable from the prompt header, undermining the "make a quick
approve/deny decision" UX permission prompts are supposed to enable.

Fix contract:
  1. ``OutboxMessage.meta`` carries ``prompt`` (= just the prompt
     header) and ``detail`` (= when present) as structured fields.
     ``msg.text`` keeps the concatenated string for backward-compat
     (CLI Panel renderer + log fallback consume it unchanged).
  2. ``InterventionWidget`` accepts a ``detail`` constructor kwarg and
     renders it as a separate Label with the ``iv-detail`` CSS class
     (italic, muted) beneath the prompt header.

Tier 2 tests use the real ``InterventionHandler.announce`` against a
real outbox queue and inspect the produced ``OutboxMessage`` shape.
Widget rendering is exercised through ``InterventionWidget.__init__``
state (= ``_detail`` attribute) rather than a full Textual app harness,
which is out of Tier 2 scope.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.chat.outbox import OutboxMessage
from reyn.tui.widgets.intervention import InterventionWidget
from reyn.user_intervention import UserIntervention


def _drain(q: asyncio.Queue) -> list[OutboxMessage]:
    items: list[OutboxMessage] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


async def _announce(iv: UserIntervention) -> OutboxMessage:
    """Run InterventionHandler.announce against a real outbox + return the msg."""
    from reyn.chat.services.intervention_handler import InterventionHandler

    q: asyncio.Queue = asyncio.Queue()

    async def put(msg: OutboxMessage) -> None:
        await q.put(msg)

    handler = InterventionHandler(
        intervention_registry=None,  # not exercised by announce()
        journal=None,                # not exercised by announce()
        event_log=None,               # not exercised by announce()
        put_outbox=put,
        append_history=lambda *_a, **_k: None,
    )
    await handler.announce(iv)
    msgs = _drain(q)
    assert len(msgs) == 1
    return msgs[0]


# ── 1. announce surfaces prompt + detail in meta ───────────────────────────


def test_announce_carries_prompt_in_meta() -> None:
    """Tier 2: meta.prompt is just the prompt string (= no detail / choices)."""
    iv = UserIntervention(
        id="iv1",
        kind="permission_request",
        prompt="Permission request — web.fetch",
        detail="web fetch: https://example.com",
    )
    msg = asyncio.run(_announce(iv))
    assert msg.meta["prompt"] == "Permission request — web.fetch"


def test_announce_carries_detail_in_meta() -> None:
    """Tier 2: meta.detail carries the resource line when present."""
    iv = UserIntervention(
        id="iv1",
        kind="permission_request",
        prompt="Permission request — web.fetch",
        detail="web fetch: https://example.com",
    )
    msg = asyncio.run(_announce(iv))
    assert msg.meta["detail"] == "web fetch: https://example.com"


def test_announce_omits_detail_when_absent() -> None:
    """Tier 2: missing detail → meta.detail key is absent (= clean payload)."""
    iv = UserIntervention(
        id="iv1",
        kind="permission_request",
        prompt="Permission request — web.fetch",
    )
    msg = asyncio.run(_announce(iv))
    assert "detail" not in msg.meta


def test_announce_keeps_msg_text_concatenated_for_backward_compat() -> None:
    """Tier 2: msg.text still contains prompt + detail + choices.

    CLI Panel renderer and the log fallback consume msg.text and have
    no awareness of meta.detail. Keeping the concatenated form means
    those paths render unchanged after the fix.
    """
    iv = UserIntervention(
        id="iv1",
        kind="permission_request",
        prompt="Permission request — web.fetch",
        detail="web fetch: https://example.com",
    )
    msg = asyncio.run(_announce(iv))
    assert "Permission request — web.fetch" in msg.text
    assert "web fetch: https://example.com" in msg.text


def test_announce_ask_user_prefixes_question_label() -> None:
    """Tier 2: ask_user kind prefixes msg.text with 'Question: '.

    meta.prompt holds the bare prompt — the 'Question: ' prefix is a
    msg.text formatting concern only.
    """
    iv = UserIntervention(
        id="iv1",
        kind="ask_user",
        prompt="What's your timezone?",
    )
    msg = asyncio.run(_announce(iv))
    assert msg.text.startswith("Question: What's your timezone?")
    # meta.prompt is the bare value — the prefix is text-formatting only.
    assert msg.meta["prompt"] == "What's your timezone?"


# ── 2. InterventionWidget renders detail as a separate Label ───────────────


def test_widget_accepts_detail_kwarg() -> None:
    """Tier 2: InterventionWidget(detail=...) stores it on the public
    ``detail`` accessor."""
    widget = InterventionWidget(
        question="Permission request — web.fetch",
        detail="web fetch: https://example.com",
        iv_id="iv1",
    )
    assert widget.detail == "web fetch: https://example.com"


def test_widget_detail_defaults_to_none() -> None:
    """Tier 2: no detail kwarg → ``detail`` is None (= no extra Label).

    Backward-compat for the (legacy) callers that don't supply detail
    yet — the widget compose() path skips the iv-detail Label when None.
    """
    widget = InterventionWidget(question="hello", iv_id="iv1")
    assert widget.detail is None


# ── 3. app-level mount path threads detail through ────────────────────────


def test_conversation_mount_intervention_accepts_detail() -> None:
    """Tier 2: ConversationView.mount_intervention propagates detail kwarg.

    Inspecting the function signature is sufficient — actual widget
    mounting requires a Textual app harness.
    """
    import inspect

    from reyn.tui.widgets.conversation import ConversationView
    sig = inspect.signature(ConversationView.mount_intervention)
    assert "detail" in sig.parameters
    # Must be a keyword-only parameter with default None to avoid
    # breaking callers that don't pass it.
    param = sig.parameters["detail"]
    assert param.default is None


def test_app_mount_intervention_accepts_detail() -> None:
    """Tier 2: ReynTUIApp._mount_intervention takes a detail kwarg.

    Same shape check as conversation — pins that the mount chain is
    fully threaded so app_outbox can forward meta.detail.
    """
    import inspect

    from reyn.tui.app import ReynTUIApp
    sig = inspect.signature(ReynTUIApp._mount_intervention)
    assert "detail" in sig.parameters
    param = sig.parameters["detail"]
    assert param.default is None


# ── 4. _iv_meta helpers stay in sync across modules ───────────────────────


def test_iv_meta_helpers_emit_same_keys() -> None:
    """Tier 2: both _iv_meta helpers (session.py + intervention_handler.py)
    produce identical meta shape for the same intervention.

    The two helpers exist as mirror copies (see docstrings); a drift
    here means OutboxMessage.meta from the two emit sites disagree and
    TUI consumers see inconsistent payloads.
    """
    from reyn.chat.services.intervention_handler import _iv_meta as handler_meta
    from reyn.chat.session import _iv_meta as session_meta

    iv = UserIntervention(
        id="iv1",
        kind="permission_request",
        prompt="Permission request — web.fetch",
        detail="web fetch: https://example.com",
        run_id="r-abc-1234",
        skill_name="web_fetch",
    )
    a = handler_meta(iv)
    b = session_meta(iv)
    assert a == b
    # Spot-check the new fields land in both:
    assert a["prompt"] == "Permission request — web.fetch"
    assert a["detail"] == "web fetch: https://example.com"
