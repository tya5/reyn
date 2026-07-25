"""Body presentation for the Textual chat surface's flowview.

:class:`ReynPresenter` fills the flowview's body cell for one display frame,
reusing the plain renderer's palette + per-kind body construction (``_CC_*`` /
``_KIND_LINE`` / ``_body_renderable``) via :func:`_body_and_background` rather
than inventing a second styling vocabulary. The gutter column is the
:class:`~reyn.interfaces.inline.textual_chat.gutter.ReynGutter`'s job.

This module is part of the TTY-only ``textual_chat`` package (imported lazily
via :mod:`reyn.interfaces.repl.client_driver`); its ``textual_flowview`` import
never reaches an always-loaded module.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console, RenderableType
from rich.text import Text
from textual_flowview import Presentation

from reyn.interfaces.repl.renderer import (
    _CC_DIM,
    _CC_ERR,
    _CC_TEXT,
    _CC_USER_BG,
    _KIND_LINE,
    _body_renderable,
    _summarize_args,
    summarize_tool_result,
)

if TYPE_CHECKING:
    from reyn.runtime.outbox import OutboxMessage


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
