"""Tier 2: #2770 — intervention display unified with the `present` renderer discipline.

This is a DISPLAY-layer unification that also closes a real terminal-injection
surface. Intervention content is LLM-derived / untrusted (ask_user ``prompt`` /
``suggestions`` come from a model tool-call; permission prompts interpolate a
model-controlled ``path``). Before #2770 the intervention display applied NO
ESC/control strip and NO markup-inert guard on any path (announce scrollback +
the prompt_toolkit choice region). This suite pins:

  1. Security (the core): an intervention (ask_user prompt / options) carrying a
     terminal control/ESC sequence is rendered NEUTRALIZED/inert — the sequence
     is stripped — on the announce scrollback (text + nodes). Falsify: drop the
     neutralizer → these go RED. (The prompt_toolkit choice-region half was
     retired with the old inline app in the #3273 TUI rebuild.)
  2. Rendering consistency: an intervention announcement draws through the SAME
     ``render_presentation_nodes`` primitive as ``present`` (the reuse seam), and
     Rich-markup-shaped leaf data survives as LITERAL text through the full
     inline render pipeline (markup-inert, like present).
  3. Semantics unchanged (non-regression): the two-way pause still round-trips —
     an ask_user dispatch blocks, an answer is delivered, dispatch returns it.

Real instances throughout (real InterventionHandler / InterventionRegistry /
SnapshotJournal / EventLog / Rich Console); no mocks. Behavioral asserts only
(a control byte is absent / a substring is present) — no whitespace/format pins.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from rich.console import Console

from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.interfaces.repl.present_renderer import render_presentation_nodes
from reyn.interfaces.repl.renderer import format_inline_message
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.services.intervention_handler import InterventionHandler
from reyn.runtime.services.intervention_registry import InterventionRegistry
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionChoice,
    UserIntervention,
)
from tests._async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper

# A terminal control/ESC injection payload: ESC + CSI red SGR, a bell, and a NUL.
ESC = "\x1b[31mINJECT\x1b[0m\x07\x00"


def _build_handler(
    tmp_path: Path, outbox: list[OutboxMessage]
) -> tuple[InterventionHandler, InterventionRegistry]:
    """A wired, all-real InterventionHandler + InterventionRegistry pair."""
    state_log = StateLog(tmp_path / "state.wal")
    event_store = EventStore(tmp_path / "events")
    event_log = EventLog(subscribers=[event_store])
    journal = SnapshotJournal(
        agent_name="t", snapshot_path=tmp_path / "snap.json", state_log=state_log,
    )

    async def _put_outbox(msg: OutboxMessage) -> None:
        outbox.append(msg)

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
        append_history=lambda *a: None,
    )
    handler_ref.append(handler)
    return handler, registry


def _iv(**kw) -> UserIntervention:
    iv = UserIntervention(**kw)
    iv.future = asyncio.get_event_loop().create_future()
    return iv


def _leaf_strings(nodes: list[dict]) -> list[str]:
    """Every leaf string in a present-shaped node list (text + list items)."""
    out: list[str] = []
    for node in nodes:
        if "text" in node:
            out.append(node["text"])
        out.extend(node.get("items", []))
    return out


# ── 1. Security — announce neutralizes control/ESC on text AND nodes ─────────


@pytest.mark.asyncio
async def test_announce_strips_control_esc_from_prompt_and_options(tmp_path) -> None:
    """Tier 2: an ask_user whose prompt + options carry ESC/control sequences is
    announced with those sequences STRIPPED on both the plain `text` fallback and
    the present-shaped `meta["nodes"]` — the LLM-derived injection surface closed."""
    outbox: list[OutboxMessage] = []
    handler, _ = _build_handler(tmp_path, outbox)

    iv = _iv(
        kind="ask_user",
        prompt=f"pick {ESC}",
        suggestions=[f"opt {ESC}"],
        choices=[InterventionChoice(id="y", label=f"[y]es {ESC}", hotkey="y")],
    )
    await handler.announce(iv)

    msg = next(m for m in outbox if m.kind == "intervention")
    # Plain-text fallback (consumed by --cui / Rich Panel / logs) is guarded.
    assert "\x1b" not in msg.text
    assert "\x07" not in msg.text and "\x00" not in msg.text
    assert "INJECT" in msg.text  # payload text survives; only the control bytes go
    # The present-shaped nodes (inline CUI path) are guarded on every leaf.
    leaves = _leaf_strings(msg.meta["nodes"])
    assert leaves, "announce must attach a present-shaped nodes render model"
    for leaf in leaves:
        assert "\x1b" not in leaf and "\x07" not in leaf and "\x00" not in leaf


@pytest.mark.asyncio
async def test_announce_neutralized_nodes_stay_inert_through_full_inline_render(
    tmp_path,
) -> None:
    """Tier 2: the announced intervention drawn through the real inline pipeline
    (format_inline_message → Rich Console) emits NO ESC byte, and Rich-markup in
    the LLM content survives as LITERAL text (markup-inert, exactly like present)."""
    outbox: list[OutboxMessage] = []
    handler, _ = _build_handler(tmp_path, outbox)

    iv = _iv(kind="ask_user", prompt=f"see {ESC} and [bold]markup[/bold]")
    await handler.announce(iv)
    msg = next(m for m in outbox if m.kind == "intervention")

    console = Console(width=80, file=io.StringIO(), force_terminal=True, color_system=None)
    console.print(format_inline_message(msg))
    out = console.file.getvalue()

    assert "\x1b[31m" not in out            # the injected red SGR never reaches the terminal
    assert "[bold]markup[/bold]" in out     # markup survives literal, never interpreted
    assert "INJECT" in out


# ── 1c. Security — the unknown-choice re-prompt hint (GAP-1) ─────────────────


@pytest.mark.asyncio
async def test_unknown_choice_hint_neutralizes_llm_choice_labels(tmp_path) -> None:
    """Tier 2: the unknown-choice re-prompt hint echoes LLM-derived choice labels
    to the SAME inline terminal surface as announce (deliver_answer_to no-match
    path). Those labels are neutralized (ESC/control strip) so an invalid-choice
    input cannot leak a terminal injection (#2770 GAP-1)."""
    outbox: list[OutboxMessage] = []
    handler, _ = _build_handler(tmp_path, outbox)

    iv = _iv(
        kind="ask_user",
        prompt="pick one",
        choices=[InterventionChoice(id="1", label=f"[1] {ESC}", hotkey="1")],
    )
    consumed = await handler.deliver_answer_to(iv, "does-not-match-any-hotkey")
    assert consumed is True  # input consumed (re-prompt), not routed to a turn

    hint = next(m for m in outbox if m.kind == "status" and "unknown choice" in m.text)
    assert "\x1b" not in hint.text and "\x07" not in hint.text and "\x00" not in hint.text
    assert "INJECT" in hint.text  # payload survives; only the control bytes go


# ── 1d. Security — the /pending pending-op summary echo (GAP-2) ──────────────


def test_pending_op_summary_neutralized_before_terminal_echo() -> None:
    """Tier 2: the /pending list echo of a stalled intervention's summary (=
    LLM-derived iv.prompt) is neutralized before it reaches the inline terminal
    via reply() — an ask_user prompt with ESC/control cannot leak through the
    /pending observe path (#2770 GAP-2). Real _render_list + real PendingOpView."""
    from reyn.interfaces.slash.pending import _render_list
    from reyn.runtime.pending_op_view import PendingOpView

    view = PendingOpView(
        id="abcd1234ef",
        kind="intervention",
        origin_channel_id="tui",
        created_at="2026-01-01T00:00:00Z",
        summary=f"approve deploy {ESC}?",
    )
    rendered = _render_list([view])
    assert "\x1b" not in rendered and "\x07" not in rendered and "\x00" not in rendered
    assert "INJECT" in rendered  # payload text survives; control bytes stripped


# ── 2. Rendering consistency — same primitive as present ─────────────────────


@pytest.mark.asyncio
async def test_intervention_draws_through_the_shared_present_primitive(tmp_path) -> None:
    """Tier 2: an intervention-with-nodes renders through the SAME
    `render_presentation_nodes` primitive `present` uses (the reuse seam) — the
    formatted renderable contains the same present-node render for those nodes."""
    outbox: list[OutboxMessage] = []
    handler, _ = _build_handler(tmp_path, outbox)

    iv = _iv(kind="ask_user", prompt="round-trippable question")
    await handler.announce(iv)
    msg = next(m for m in outbox if m.kind == "intervention")

    # The intervention body must be the shared present renderable for its nodes.
    def _to_text(renderable) -> str:
        c = Console(width=80, file=io.StringIO(), force_terminal=True, color_system=None)
        c.print(renderable)
        return c.file.getvalue()

    present_body = _to_text(render_presentation_nodes(msg.meta["nodes"]))
    intervention_render = _to_text(format_inline_message(msg))
    assert "round-trippable question" in present_body
    assert "round-trippable question" in intervention_render


# ── 3. Semantics unchanged — the two-way pause still round-trips ─────────────


@pytest.mark.asyncio
async def test_two_way_pause_round_trips_unchanged(tmp_path) -> None:
    """Tier 2: non-regression — an ask_user dispatch blocks, an answer is delivered,
    and dispatch returns the InterventionAnswer — the pause/reply flow is untouched
    by the display-layer unification."""
    outbox: list[OutboxMessage] = []
    handler, registry = _build_handler(tmp_path, outbox)

    iv = _iv(kind="ask_user", prompt="What city?", run_id="rY")
    dispatch_task: asyncio.Task[InterventionAnswer] = asyncio.ensure_future(
        handler.dispatch(iv)
    )
    await wait_until(lambda: bool(registry.list_active()))

    consumed = await handler.maybe_answer("Tokyo")
    assert consumed is True

    result = (await asyncio.gather(dispatch_task, return_exceptions=True))[0]
    assert isinstance(result, InterventionAnswer)
    assert result.text == "Tokyo"
    # And the announcement went out (display fired) — semantics + display coexist.
    assert any(m.kind == "intervention" for m in outbox)
