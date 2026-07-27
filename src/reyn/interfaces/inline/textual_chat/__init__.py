"""Textual conversation-pane app for the interactive TTY chat surface.

This is the TTY-path chat surface: a :class:`textual.app.App` that OWNS both
input (a multiline :class:`~reyn.interfaces.inline.textual_chat.chrome.Composer`)
and output (a retained :class:`~textual_flowview.FlowModel` rendered through a
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
inventing a second styling vocabulary:
:class:`~reyn.interfaces.inline.textual_chat.presenter.ReynPresenter` fills the
body cell and
:class:`~reyn.interfaces.inline.textual_chat.gutter.ReynGutter` fills the flowview
gutter column, the split flowview's presenter/decorator protocol expects.

Phase 3 adds the bottom-chrome tab-drawer. Below the composer sit two slim rows —
a :class:`~reyn.interfaces.inline.textual_chat.chrome.StatusLine` of
``model │ agent │ cost │ ctx`` values and a focusable
:class:`~reyn.interfaces.inline.textual_chat.chrome.MenuBar` (a ``Tabs`` row:
``Model Agent History Cost Ctx Menu Help``) — plus a
:class:`~textual.widgets.ContentSwitcher` drawer that is collapsed by default and
expands DOWNWARD when a menu item is opened. Focus flows ``↓`` from the composer's
last line into the menu, ``← →`` move the highlight, ``Enter`` opens the
highlighted item's drawer, and ``↑``/``Esc`` close it and return focus to the
composer (arrow-move alone never opens — opening is an explicit Enter).
Interactive panes are Textual :class:`~textual.widgets.OptionList` widgets
(Model/Agent/History/Menu); static readouts are plain Rich
:class:`~textual.widgets.Static` (Cost/Ctx/Help). Phase 4 wires every pane to its
canonical reyn source — the status snapshot (model/agent/cost/ctx), the slash
``REGISTRY`` (menu), the live conversation (history), and the app BINDINGS (help)
— with the enumerating panes (Model/Agent/Menu) deriving their FULL set from the
registry (never a curated subset). See
:func:`~reyn.interfaces.inline.textual_chat.chrome.pane_payload`.

Package layout (Phase 3F split — this ``__init__`` is the single lazy import
boundary and re-exports the public API):

- :mod:`~reyn.interfaces.inline.textual_chat.app` — ``TextualChatApp`` +
  ``run_textual_chat`` (wiring, frame pump, blink timer, drawer control).
- :mod:`~reyn.interfaces.inline.textual_chat.presenter` — ``ReynPresenter`` +
  ``_body_and_background`` (body cell construction).
- :mod:`~reyn.interfaces.inline.textual_chat.gutter` — ``ReynGutter`` (LEFT,
  state-coloured marker) + ``ReynTimingGutter`` (RIGHT, per-entry elapsed
  time — Phase ④, #3283) + running-frame constants.
- :mod:`~reyn.interfaces.inline.textual_chat.chrome` — ``Composer``,
  ``StatusLine``, ``MenuBar``, ``_MENU_TABS``, and the pure pane formatters
  (``pane_payload`` / ``status_line_text`` / ``build_drawer_pane``) that derive
  each pane's rows from its canonical source (input + bottom-chrome widgets).
- :mod:`~reyn.interfaces.inline.textual_chat.intervention_panel` —
  ``InterventionPanel``, the grouped panel widget (#3299 P1) an intervention's
  interaction (closed-set select / free-text answer) is answered through —
  the FlowView only ever shows a thin pending/answered placeholder.

Import boundary (load-bearing): this package imports :mod:`textual` and
:mod:`textual_flowview` at import time (through its submodules), so it must only
ever be imported on the TTY path — :func:`~reyn.interfaces.repl.client_driver.run_chat_client`
imports it lazily inside its inline-interactive branch. The plain / ``--cui`` /
non-TTY / CI paths never import it, so they stay green even if flowview is absent.
"""
from __future__ import annotations

from .app import TextualChatApp, run_textual_chat
from .chrome import Composer, MenuBar, StatusLine
from .gutter import ReynGutter, ReynTimingGutter
from .intervention_panel import InterventionPanel
from .presenter import ReynPresenter, _body_and_background

__all__ = [
    "Composer",
    "InterventionPanel",
    "MenuBar",
    "ReynGutter",
    "ReynPresenter",
    "ReynTimingGutter",
    "StatusLine",
    "TextualChatApp",
    "_body_and_background",
    "run_textual_chat",
]
