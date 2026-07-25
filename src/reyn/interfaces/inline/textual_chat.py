"""Textual conversation-pane app for the interactive TTY chat surface.

This is the TTY-path chat surface: a :class:`textual.app.App` that OWNS both
input (a multiline :class:`Composer`) and output (a retained
:class:`~textual_flowview.FlowModel` rendered through a
:class:`~textual_flowview.FlowView`). It is deliberately NOT a
:class:`~reyn.interfaces.repl.renderer.ChatRenderer` subclass: the plain
renderer is an incremental *print* paradigm with no retained model, whereas this
app keeps every conversation entry in a model and RE-PRESENTS the visible ones on
every resize — the reflow the plain scrollback cannot do.

The app is fed from the SAME ``transport.frames()`` stream the plain output loop
consumes (:mod:`reyn.interfaces.repl.stream_client`): a Textual worker drains the
stream and appends each display frame to the model, so the on-screen turn
sequence is structurally identical to the plain renderer's — only the drawing
differs. Composer submissions route back through the same transport send seam.

Presentation reuses reyn's own Claude-Code palette and per-kind line table
(:data:`~reyn.interfaces.repl.renderer._CC_TEXT` … / ``_KIND_LINE``) rather than
inventing a second styling vocabulary: :class:`ReynPresenter` fills the body cell
and :class:`ReynGutter` fills the flowview gutter column, the split flowview's
presenter/decorator protocol expects.

Import boundary (load-bearing): this module imports :mod:`textual` and
:mod:`textual_flowview` at top level, so it must only ever be imported on the TTY
path — :func:`~reyn.interfaces.repl.client_driver.run_chat_client` imports it
lazily inside its inline-interactive branch. The plain / ``--cui`` / non-TTY /
CI paths never import it, so they stay green even if flowview is absent.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from rich.console import Console, RenderableType
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Static, TextArea
from textual_flowview import (
    Anchor,
    Entry,
    EntryState,
    FlowModel,
    FlowView,
    Presentation,
)

from reyn.interfaces.repl.renderer import (
    _CC_DIM,
    _CC_DONE,
    _CC_ERR,
    _CC_TEXT,
    _CC_USER_BG,
    _CC_WARN,
    _KIND_LINE,
    _body_renderable,
    _summarize_args,
    summarize_tool_result,
)
from reyn.interfaces.transport.frames import FrameTag

if TYPE_CHECKING:
    from textual.timer import Timer

    from reyn.interfaces.repl.read_model import ChatReadModel
    from reyn.interfaces.transport.client_transport import ClientTransport
    from reyn.runtime.outbox import OutboxMessage

logger = logging.getLogger(__name__)

# Display kinds that are command-UI / control sentinels, not conversation
# content. The plain output loop consumes them as signals; the Phase-1
# conversation pane simply skips them (their surfaces land in later phases).
# ``__end__`` is handled by the pump loop (it stops the app), so it is not here.
_SKIP_KINDS = frozenset(
    {
        "__copy_last_reply__",
        "__rewind_list__",
        "__attach_request__",
        "__session_switch_request__",
    }
)


def _body_and_background(msg: "OutboxMessage") -> "tuple[RenderableType, str | None]":
    """The body renderable + optional full-row background for one display frame.

    Reuses the plain renderer's per-kind body construction (markdown for the
    agent reply, the tool-summary helpers for tool rows, the ``_KIND_LINE`` body
    style otherwise) so a frame reads the same here as in the plain scrollback.
    The user's own line carries its background via ``Presentation.background``
    (flowview paints it edge to edge across gutter + body), matching the plain
    renderer's faint user block without a hand-rolled grid. A FAILURE row
    (``tool_call_failed`` / ``error`` / a ``tool_call_completed`` whose summary
    is an ``✗`` failure) carries ``background=_CC_ERR`` so the whole row is
    tinted coral edge to edge — CC's block-tint of a failed tool (Phase 2).
    """
    kind = msg.kind
    meta = msg.meta or {}
    if kind == "presentation":
        from reyn.interfaces.repl.present_renderer import render_presentation_nodes
        return render_presentation_nodes(meta.get("nodes", [])), None
    if kind == "intervention" and meta.get("nodes") is not None:
        from reyn.interfaces.repl.present_renderer import render_presentation_nodes
        return render_presentation_nodes(meta["nodes"]), None
    if kind == "tool_call_started":
        tool = str(meta.get("tool", msg.text))
        args = _summarize_args(meta.get("args"))
        return Text.assemble((tool, "bold"), (f"({args})", _CC_DIM)), None
    if kind == "tool_call_completed":
        summary = summarize_tool_result(meta.get("tool"), meta.get("result"))
        failed = summary.startswith("✗")
        style = _CC_ERR if failed else _CC_DIM
        return Text(summary, style=style), (_CC_ERR if failed else None)
    if kind == "tool_call_failed":
        err = meta.get("error_message") or meta.get("error_kind") or msg.text
        return Text(f"✗ {err}", style=_CC_ERR), _CC_ERR
    line = _KIND_LINE.get(kind)
    body_style = line[2] if line else _CC_TEXT
    body = _body_renderable(kind, msg.text or " ", body_style)
    if kind == "user":
        background = _CC_USER_BG
    elif kind == "error":
        background = _CC_ERR
    else:
        background = None
    return body, background


# EntryState → gutter colour (Phase 2 state-color gutter). The CC state
# palette: RUNNING amber, SUCCESS green, ERROR coral, DEFAULT/CANCELLED dim.
# Applied by :meth:`ReynGutter.decorate` when an entry carries a non-DEFAULT
# lifecycle state; DEFAULT entries fall back to their kind colour.
_STATE_COLOR: "dict[EntryState, str]" = {
    EntryState.DEFAULT: _CC_DIM,
    EntryState.RUNNING: _CC_WARN,
    EntryState.SUCCESS: _CC_DONE,
    EntryState.ERROR: _CC_ERR,
    EntryState.CANCELLED: _CC_DIM,
}

# Running-blink frames: a two-phase ●/○ pulse cycled by the app-side timer's
# shared frame counter (:meth:`TextualChatApp._advance_blink`). The blink lives
# ENTIRELY in reyn — the counter + timer in the app, the frame selection here;
# textual-flowview is never modified or forked.
_RUNNING_FRAMES = ("●", "○")


def _gutter_glyph_color(msg: "OutboxMessage") -> "tuple[str, str]":
    """The gutter glyph + kind-colour for one display frame, keyed off ``_KIND_LINE``.

    Mirrors the plain renderer's marker column: the ``_KIND_LINE`` glyph (its
    leading non-space char) for message-y kinds, the ``●`` tool-header /  ``⎿``
    tool-result markers otherwise. The colour returned here is the KIND colour;
    a non-DEFAULT :class:`EntryState` overrides it in :meth:`ReynGutter.decorate`
    (state-driven colour, Phase 2). Kept cheap — ``decorate`` runs on every repaint.
    """
    kind = msg.kind
    if kind == "tool_call_started":
        return "●", _CC_TEXT
    if kind == "tool_call_completed":
        return "⎿", _CC_DIM
    if kind == "tool_call_failed":
        return "⎿", _CC_ERR
    line = _KIND_LINE.get(kind)
    if line is None:
        return "", _CC_DIM
    glyph = line[0].strip()[:1]
    return glyph, line[1]


class ReynPresenter:
    """Turns a reyn display frame into a body :class:`Presentation` sized to
    ``width`` — reusing the plain renderer's palette + per-kind body construction
    (``_CC_*`` / ``_KIND_LINE`` / ``_body_renderable``), never a second styling
    vocabulary. The gutter is the :class:`ReynGutter`'s job."""

    def __init__(self) -> None:
        # A private probe console for measuring wrapped height at a given width.
        self._probe = Console()

    def _measure(self, renderable: RenderableType, width: int) -> int:
        self._probe.size = (max(width, 1), 200)
        return max(
            len(
                self._probe.render_lines(
                    renderable, self._probe.options.update_width(max(width, 1))
                )
            ),
            1,
        )

    async def present(self, item: "OutboxMessage", width: int) -> Presentation:
        body, background = _body_and_background(item)
        return Presentation(
            height=self._measure(body, width),
            renderable=body,
            background=background,
        )


class ReynGutter:
    """Fills the flowview gutter column with a STATE-COLOURED marker (Phase 2).

    The glyph is kind-driven (``❯`` user, ``●`` assistant / tool-header, ``⎿``
    tool-result — via :func:`_gutter_glyph_color`); the COLOUR is driven by the
    entry's :class:`EntryState`: RUNNING amber, SUCCESS green, ERROR coral
    (:data:`_STATE_COLOR`). A DEFAULT-state entry keeps its kind colour, so plain
    message rows are unchanged from Phase 1.

    While an entry is ``RUNNING`` its marker BLINKS: the glyph cycles through
    :data:`_RUNNING_FRAMES` selected by ``blink_frame()`` — a shared counter
    advanced by the app-side timer (:meth:`TextualChatApp._advance_blink`). The
    decorator only READS the counter; the timer, the counter, and the redraw
    trigger all live in the reyn app. textual-flowview is never modified.
    ``decorate`` stays synchronous + cheap (it runs on every gutter repaint)."""

    def __init__(self, blink_frame: "Callable[[], int]" = lambda: 0) -> None:
        self._blink_frame = blink_frame

    def decorate(self, entry: "Entry[OutboxMessage]", width: int, height: int) -> RenderableType:
        glyph, kind_color = _gutter_glyph_color(entry.item)
        state = entry.state
        if state is EntryState.RUNNING:
            glyph = _RUNNING_FRAMES[self._blink_frame() % len(_RUNNING_FRAMES)]
            color = _CC_WARN
        elif state is EntryState.DEFAULT:
            color = kind_color
        else:
            color = _STATE_COLOR.get(state, kind_color)
        return Text(glyph.ljust(width), style=color)


class Composer(TextArea):
    """Multi-line Claude-Code-style input: **Enter submits**, **Shift+Enter**
    inserts a newline (the inverse of ``TextArea``'s default), auto-growing up to
    ``MAX_ROWS`` then internally scrolling. Every other key falls through to the
    base ``TextArea`` bindings unchanged."""

    MAX_ROWS = 6

    class Submitted(Message):
        """Posted when the user presses Enter with non-blank content."""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def on_mount(self) -> None:
        self.show_line_numbers = False
        self._sync_height()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if self.text.strip():
                self.post_message(self.Submitted(self.text))
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self._replace_via_keyboard("\n", start, end)
            return
        await super()._on_key(event)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._sync_height()

    def _sync_height(self) -> None:
        wrapped_rows = max(self.wrapped_document.height, 1)
        self.styles.height = min(wrapped_rows, self.MAX_ROWS)

    def clear_and_reset(self) -> None:
        self.text = ""
        self._sync_height()


class TextualChatApp(App):
    """The TTY conversation pane: a FlowView of the live conversation + a
    Composer, both fed/served by one :class:`ClientTransport`.

    The app drains ``transport.frames()`` in a worker, appending each display
    frame to the retained model (event frames are consumed but not yet drawn);
    a Composer submit routes back through the transport. The user's own line is
    NOT echoed locally — it returns as a ``kind="user"`` frame on the same
    stream, so the model is fed entirely from frames and stays equivalent to the
    plain renderer's turn sequence.

    Phase 2 adds state-coloured gutters: a tool-call row transitions RUNNING →
    SUCCESS/ERROR (amber → green/coral) as its correlated frames arrive, a
    failed row is tinted coral edge-to-edge, and RUNNING rows blink via an
    app-side timer (:meth:`_advance_blink`). The blink is additive — neutering
    the timer leaves a static, correct gutter.
    """

    #: Seconds between running-blink frames (app-side; textual-flowview unmodified).
    BLINK_INTERVAL = 0.5

    CSS = """
    Screen { layout: vertical; }
    FlowView {
        height: 1fr;
        scrollbar-size-vertical: 0;
    }
    #inputrow {
        height: auto;
        max-height: 8;
        margin-top: 1;
        border-top: solid #3d434f;
        border-bottom: solid #3d434f;
    }
    #inputgutter {
        width: 2;
        height: auto;
        color: $text-muted;
    }
    Composer {
        height: 3;
        max-height: 6;
        border: none;
        padding: 0;
    }
    """

    def __init__(
        self,
        *,
        transport: "ClientTransport",
        read_model: "ChatReadModel | None" = None,
        agent_name: str = "default",
        config=None,
    ) -> None:
        super().__init__()
        self._transport = transport
        self._read_model = read_model
        self._agent_name = agent_name
        self._config = config
        self.conversation: "FlowModel[OutboxMessage]" = FlowModel()
        # Running tool-call entries keyed by op_id (== the dispatcher's
        # deterministic args_hash, meta["op_id"]) so a later completion/failure
        # frame transitions the SAME entry RUNNING → SUCCESS/ERROR (CC parity).
        self._running_tools: "dict[object, Entry[OutboxMessage]]" = {}
        # Shared blink frame counter read by ReynGutter; advanced by the timer.
        self._blink_count = 0
        self._blink_timer: "Timer | None" = None

    def compose(self) -> ComposeResult:
        yield FlowView(
            model=self.conversation,
            presenter=ReynPresenter(),
            decorator=ReynGutter(blink_frame=lambda: self._blink_count),
            gutter_width=2,
            spacing=1,
            anchor=Anchor.STICKY_BOTTOM,
        )
        with Horizontal(id="inputrow"):
            yield Static("❯", id="inputgutter")
            yield Composer(
                placeholder="Type a message — Enter to send, Shift+Enter for a newline…"
            )

    def on_mount(self) -> None:
        # The running-blink timer starts PAUSED and is resumed only while a
        # tool call is RUNNING (and paused again when none remain) — it never
        # spins on an idle conversation. The blink is app-side + ADDITIVE:
        # neutering ``_advance_blink`` freezes the frame to a static gutter
        # without affecting correctness (see the Phase-2 strip gate).
        self._blink_timer = self.set_interval(
            self.BLINK_INTERVAL, self._advance_blink, pause=True
        )
        self.run_worker(self._pump_frames(), name="frames", exclusive=True)
        self.query_one(Composer).focus()

    def _advance_blink(self) -> None:
        """One blink tick: advance the shared frame counter and redraw ONLY the
        running entries' gutters (``set_metadata`` is flowview's gutter-only
        redraw primitive — it never re-presents the body). Pauses itself when no
        entry is RUNNING, so the timer does not spin on an idle conversation."""
        self._blink_count += 1
        for entry in list(self._running_tools.values()):
            entry.set_metadata("_blink", self._blink_count)
        if not self._running_tools and self._blink_timer is not None:
            self._blink_timer.pause()

    def _track_tool_state(self, msg: "OutboxMessage", entry: "Entry[OutboxMessage]") -> None:
        """Drive the Phase-2 lifecycle state of tool-call / error rows.

        A ``tool_call_started`` row (with an ``op_id`` correlation key) becomes
        RUNNING and starts blinking; the matching ``tool_call_completed`` /
        ``tool_call_failed`` frame transitions that SAME entry to SUCCESS/ERROR
        (its gutter goes amber → green/coral). ``error`` rows go straight to
        ERROR. Frames without an ``op_id`` carry no state (DEFAULT) — they append
        exactly as in Phase 1, so the plain-fallback turn sequence is unchanged."""
        kind = msg.kind
        meta = msg.meta or {}
        op_id = meta.get("op_id")
        if kind == "tool_call_started":
            if op_id is not None:
                entry.set_state(EntryState.RUNNING)
                self._running_tools[op_id] = entry
                if self._blink_timer is not None:
                    self._blink_timer.resume()
        elif kind == "tool_call_completed":
            summary = summarize_tool_result(meta.get("tool"), meta.get("result"))
            started = self._running_tools.pop(op_id, None) if op_id is not None else None
            if started is not None:
                started.set_state(
                    EntryState.ERROR if summary.startswith("✗") else EntryState.SUCCESS
                )
            self._maybe_pause_blink()
        elif kind == "tool_call_failed":
            started = self._running_tools.pop(op_id, None) if op_id is not None else None
            if started is not None:
                started.set_state(EntryState.ERROR)
            entry.set_state(EntryState.ERROR)
            self._maybe_pause_blink()
        elif kind == "error":
            entry.set_state(EntryState.ERROR)

    def _maybe_pause_blink(self) -> None:
        if not self._running_tools and self._blink_timer is not None:
            self._blink_timer.pause()

    async def _pump_frames(self) -> None:
        """Drain the transport frame stream into the retained model.

        Display frames append to the model (skipping command-UI sentinels);
        ``__end__`` stops the app (the session closed). Event frames are the
        working-indicator path — consumed but not yet drawn. Each appended entry
        is then handed to :meth:`_track_tool_state` for its Phase-2 lifecycle
        colour. A single frame's presentation failure must not kill the pump, so
        append is guarded.
        """
        try:
            async for frame in self._transport.frames():
                if frame.tag is FrameTag.EVENT:
                    continue
                msg = frame.message
                if msg.kind == "__end__":
                    break
                if msg.kind in _SKIP_KINDS:
                    continue
                try:
                    entry = self.conversation.append(msg)
                except Exception:
                    logger.exception(
                        "textual chat: append failed for frame kind=%r", msg.kind
                    )
                    continue
                try:
                    self._track_tool_state(msg, entry)
                except Exception:
                    logger.exception(
                        "textual chat: state tracking failed for kind=%r", msg.kind
                    )
        finally:
            self.exit()

    async def on_composer_submitted(self, event: "Composer.Submitted") -> None:
        text = event.value.strip()
        self.query_one(Composer).clear_and_reset()
        if not text:
            return
        if text in {"/quit", "/exit"}:
            await self._transport.shutdown()
            self.exit()
            return
        await self._submit(text)

    async def _submit(self, text: str) -> None:
        """Route one submitted line through the transport send seam.

        A pending free-text intervention (no choices) takes the answer path;
        everything else is an ordinary new turn. Errors are contained and
        surfaced as an error frame the pump renders — a silent input drop is the
        worst failure for a chat box.
        """
        try:
            head = self._transport.pending_intervention_head()
            if head is not None and not getattr(head, "choices", None):
                await self._transport.answer_intervention_text(text)
                return
            await self._transport.submit_user_text(text)
        except Exception as exc:
            logger.exception("textual chat: submit failed")
            from reyn.runtime.outbox import OutboxMessage
            detail = f"{type(exc).__name__}: {exc}"
            try:
                self._transport.put_display(
                    OutboxMessage(
                        kind="error", text=f"input could not be submitted: {detail}"
                    )
                )
            except Exception:
                pass


async def run_textual_chat(
    *,
    transport: "ClientTransport",
    read_model: "ChatReadModel | None" = None,
    agent_name: str = "default",
    config=None,
) -> None:
    """Run the TTY conversation-pane app until the user quits or the stream ends.

    Runs ``inline=True`` so the terminal's pre-launch scrollback is preserved
    above the app region (the ADR's Phase-0-validated inline mode) rather than
    swapping to the alternate screen. Returns so the driver's caller can tear the
    transport down + print the cost summary.
    """
    app = TextualChatApp(
        transport=transport,
        read_model=read_model,
        agent_name=agent_name,
        config=config,
    )
    await app.run_async(inline=True)


__all__ = [
    "Composer",
    "ReynGutter",
    "ReynPresenter",
    "TextualChatApp",
    "run_textual_chat",
]
