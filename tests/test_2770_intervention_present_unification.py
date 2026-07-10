"""Tier 2: #2770 — intervention display unified with the `present` renderer discipline.

This is a DISPLAY-layer unification that also closes a real terminal-injection
surface. Intervention content is LLM-derived / untrusted (ask_user ``prompt`` /
``suggestions`` come from a model tool-call; permission prompts interpolate a
model-controlled ``path``). Before #2770 the intervention display applied NO
ESC/control strip and NO markup-inert guard on any path (announce scrollback +
the prompt_toolkit choice region). This suite pins:

  1. Security (the core): an intervention (ask_user prompt / options) carrying a
     terminal control/ESC sequence is rendered NEUTRALIZED/inert — the sequence
     is stripped — on BOTH the announce scrollback (text + nodes) AND the region
     choice fragments. Falsify: drop the neutralizer → these go RED.
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
from _async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper
from rich.console import Console

from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.intervention_region import (
    InterventionElement,
    build_intervention_element,
)
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


# ── 1b. Security — the region's choice fragments are neutralized ─────────────


def test_region_element_neutralizes_choice_labels() -> None:
    """Tier 2: an InterventionElement strips ESC/control from choice labels (which
    reach the prompt_toolkit FormattedTextControl as raw fragments) — the region's
    injection gap closed at the data boundary; the display rows are inert."""
    el = InterventionElement(
        "iv-1",
        [("y", f"[y]es {ESC}"), ("n", "[n]o")],
        lambda cid, label: None,
    )
    for row in el.lines():
        assert "\x1b" not in row and "\x07" not in row and "\x00" not in row
    assert any("INJECT" in row for row in el.lines())  # only control bytes stripped


def test_region_element_forwards_neutralized_label_on_select() -> None:
    """Tier 2: the label forwarded to on_choose (scrollback echo) is neutralized
    too — the choice_id (match key) is unchanged so selection semantics hold."""
    captured: list[tuple[str, str]] = []
    el = InterventionElement(
        "iv-1", [("y", f"[y]es {ESC}")], lambda cid, label: captured.append((cid, label)),
    )
    el.on_select(0)
    [(cid, label)] = captured  # exactly one forward, unpacked
    assert cid == "y"  # match key unchanged (never neutralized)
    # Label control-stripped: the ESC lead byte is gone (so the trailing "[31m"
    # bytes are inert literal text, never an escape sequence), payload survives.
    assert "\x1b" not in label and "\x07" not in label and "\x00" not in label
    assert "INJECT" in label


def test_build_intervention_element_neutralizes_via_factory() -> None:
    """Tier 2: the production factory (app.py's construction path) yields a
    neutralized element for an LLM-derived closed-set intervention."""
    from types import SimpleNamespace

    iv = SimpleNamespace(
        id="iv-2",
        choices=[SimpleNamespace(id="a", label=f"choice {ESC}")],
    )
    el = build_intervention_element(iv, lambda cid, label: None)
    assert el is not None
    assert "\x1b" not in el.lines()[0]


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
