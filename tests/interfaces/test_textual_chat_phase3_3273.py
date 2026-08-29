"""Phase 3 TUI-rebuild gates (#3273): the bottom-chrome tab-drawer.

These pin the architect-specified Phase-3 invariants:

- **collapsed-by-default** (Tier 2b): the drawer is hidden on mount — the default
  chrome is just the two slim rows (status-values line + focusable menu row).
- **focus flow** (Tier 2b): ``↓`` from the composer focuses the menu; ``← →``
  move the highlight WITHOUT opening; ``Enter`` opens the highlighted item's
  drawer downward (``ContentSwitcher.current`` set + visible); ``↑``/``Esc`` close
  it and return focus to the composer.
- **edge-to-edge** (Tier 2c): the composer and the drawer carry no side borders,
  and there is no separator rule between the menu row and its drawer — a
  structural check of the mounted widgets' computed styles.
- **import isolation preserved** (Tier 2c): the extra ``textual`` widgets the
  chrome imports at module top level do NOT leak onto the plain / non-TTY path —
  it stays green with ``textual`` / ``textual_flowview`` unimportable (the
  flowview/textual import remains lazy + TTY-only).

All use real instances (a concrete :class:`ScriptedTransport`, the real app +
pilot) — no mocks — per the testing policy.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from tests._support.paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT


class ScriptedTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport`. ``end=False`` keeps the stream
    open so the app under test stays mounted for focus/style inspection."""

    def __init__(self, messages: "list[OutboxMessage]", *, end: bool = False) -> None:
        self._messages = list(messages)
        self._end = end
        self.submitted: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        for msg in self._messages:
            yield DisplayFrame(msg)
        if self._end:
            yield DisplayFrame(OutboxMessage(kind="__end__", text=""))
        else:
            await asyncio.Event().wait()

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        self._messages.append(msg)

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _edge_is_none(edge: object) -> bool:
    """Textual's computed ``styles.border_<edge>`` is a ``(type, color)`` tuple;
    ``type`` is falsy / ``"none"`` when there is no border on that edge."""
    edge_type = edge[0] if isinstance(edge, tuple) else edge
    return edge_type in ("", "none", None)


# ── Gate 1: collapsed-by-default ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drawer_collapsed_by_default() -> None:
    """Tier 2b: on mount the drawer is hidden and empty — the default bottom
    chrome is just the two slim rows (a StatusLine + a focusable MenuBar). The
    ContentSwitcher's ``display`` is False and its ``current`` is None."""
    from textual.widgets import ContentSwitcher

    from reyn.interfaces.inline.textual_chat import MenuBar, StatusLine, TextualChatApp

    app = TextualChatApp(transport=ScriptedTransport([], end=False))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        drawer = app.query_one("#drawer", ContentSwitcher)
        assert drawer.display is False, "drawer should be collapsed on mount"
        assert drawer.current is None, "no drawer pane should be selected on mount"
        # The two slim rows are present.
        assert app.query_one(StatusLine) is not None
        assert app.query_one(MenuBar) is not None


# ── Gate 2: focus flow (↓ focus / ←→ move / Enter open / ↑ or Esc close) ──────

@pytest.mark.asyncio
async def test_focus_flow_arrow_moves_without_opening_enter_opens_esc_closes() -> None:
    """Tier 2b: the full focus flow. ``↓`` from the composer focuses the menu;
    ``→`` moves the highlight but does NOT open the drawer; ``Enter`` opens the
    highlighted item's drawer downward (ContentSwitcher.current set + visible);
    ``Esc`` closes it and returns focus to the composer."""
    from textual.widgets import ContentSwitcher

    from reyn.interfaces.inline.textual_chat import Composer, MenuBar, TextualChatApp

    app = TextualChatApp(transport=ScriptedTransport([], end=False))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()

        # ↓ from the composer's (only) line focuses the menu row.
        await pilot.press("down")
        await pilot.pause()
        assert isinstance(app.focused, MenuBar), f"↓ did not focus the menu: {app.focused!r}"

        drawer = app.query_one("#drawer", ContentSwitcher)
        first_active = app.query_one(MenuBar).active

        # → moves the highlight but must NOT open the drawer (arrow != open).
        await pilot.press("right")
        await pilot.pause()
        moved_active = app.query_one(MenuBar).active
        assert moved_active != first_active, "→ did not move the menu highlight"
        assert drawer.display is False, "arrow-move opened the drawer (must be explicit Enter)"
        assert drawer.current is None

        # Enter opens the highlighted item's drawer DOWNWARD.
        await pilot.press("enter")
        await pilot.pause()
        assert drawer.display is True, "Enter did not open the drawer"
        assert drawer.current == moved_active, "opened drawer shows the wrong pane"

        # Esc closes and returns focus to the composer (works even though focus
        # is INSIDE the drawer — the app-level binding is the fallback).
        await pilot.press("escape")
        await pilot.pause()
        assert drawer.display is False, "Esc did not close the drawer"
        assert drawer.current is None
        assert isinstance(app.focused, Composer), f"Esc did not refocus composer: {app.focused!r}"


@pytest.mark.asyncio
async def test_up_from_menu_returns_to_composer_without_opening() -> None:
    """Tier 2b: ``↑`` from the focused (but un-opened) menu row hands focus back
    to the composer and leaves the drawer collapsed — the reverse of the ``↓``
    step, and never an implicit open."""
    from textual.widgets import ContentSwitcher

    from reyn.interfaces.inline.textual_chat import Composer, MenuBar, TextualChatApp

    app = TextualChatApp(transport=ScriptedTransport([], end=False))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert isinstance(app.focused, MenuBar)

        await pilot.press("up")
        await pilot.pause()
        drawer = app.query_one("#drawer", ContentSwitcher)
        assert drawer.display is False, "↑ opened the drawer (must not)"
        assert isinstance(app.focused, Composer), f"↑ did not refocus composer: {app.focused!r}"


# ── Gate 3: edge-to-edge (no side borders, no menu↔drawer separator) ──────────

@pytest.mark.asyncio
async def test_edge_to_edge_no_side_borders_and_no_menu_drawer_separator() -> None:
    """Tier 2c: the chrome is edge-to-edge. The composer and every drawer
    OptionList carry no side borders, and there is no separator rule between the
    menu row and its drawer (MenuBar has no bottom border, the drawer no top
    border) — inspected on the mounted widgets' computed styles, not the CSS text."""
    from textual.widgets import ContentSwitcher, OptionList

    from reyn.interfaces.inline.textual_chat import Composer, MenuBar, TextualChatApp

    app = TextualChatApp(transport=ScriptedTransport([], end=False))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        # The composer is borderless on all four edges (edge-to-edge input).
        composer = app.query_one(Composer)
        for edge in ("top", "right", "bottom", "left"):
            assert _edge_is_none(getattr(composer.styles, f"border_{edge}")), (
                f"composer has a {edge} border; must be edge-to-edge"
            )

        # Drawer panes (interactive OptionLists) have their side frames stripped.
        for opt in app.query(OptionList):
            for edge in ("left", "right"):
                assert _edge_is_none(getattr(opt.styles, f"border_{edge}")), (
                    f"drawer OptionList {opt.id!r} has a {edge} border; must be edge-to-edge"
                )

        # No separator between the menu row and its drawer: neither the MenuBar's
        # bottom edge nor the drawer's top edge carries a rule.
        menubar = app.query_one(MenuBar)
        drawer = app.query_one("#drawer", ContentSwitcher)
        assert _edge_is_none(menubar.styles.border_bottom), "menu row has a bottom separator"
        assert _edge_is_none(drawer.styles.border_top), "drawer has a top separator vs the menu row"


# ── Gate 4: import isolation preserved (chrome is TTY-only) ───────────────────

# The Phase-1 witness proves the plain path survives ``textual`` absence; Phase 3
# adds MORE top-level textual imports (ContentSwitcher/OptionList/Tabs/Tab/Widget)
# to ``textual_chat``, so re-run the strip in a clean subprocess to prove those
# additions did NOT leak onto the always-loaded plain path. Same shape as the
# Phase-1 witness (a subprocess is required so cached in-process imports cannot
# mask a top-level-import regression).
_ISOLATION_SUBPROCESS = '''
import sys


class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("textual", "textual_flowview"):
            raise ModuleNotFoundError("blocked for isolation test: " + name)
        return None


sys.meta_path.insert(0, _Block())

import reyn.interfaces.repl.client_driver  # noqa: E402,F401
import reyn.interfaces.repl.stream_client  # noqa: E402,F401
import reyn.interfaces.cli.commands.chat  # noqa: E402,F401

assert "textual_flowview" not in sys.modules, "flowview imported at module load"
assert "textual" not in sys.modules, "textual imported at module load"
print("ISOLATION_OK")
'''


def test_phase3_chrome_imports_stay_tty_only(out_of_process_reyn) -> None:
    """Tier 2c: with ``textual`` / ``textual_flowview`` unimportable from a clean
    interpreter, the plain / non-TTY path still imports green — Phase 3's extra
    top-level textual widget imports (ContentSwitcher/OptionList/Tabs/…) live in
    ``textual_chat``, which stays lazily imported on the TTY path only. Runs the
    strip in a subprocess (see the module comment) and asserts ``ISOLATION_OK``.

    ``out_of_process_reyn`` (#5028): same sibling gap as
    ``test_textual_chat_phase1_3273.py::test_plain_path_survives_flowview_absence``
    (fixed by #5029) — this subprocess re-resolves ``reyn`` from the ambient
    venv, not from pytest's own ``pythonpath``, which can silently be a
    DIFFERENT checkout's ``src`` in a git worktree. Pinning the fixture's
    returned root as ``PYTHONPATH`` makes it read the SAME ``reyn`` this test
    imported."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONPATH": out_of_process_reyn}
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    proc = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SUBPROCESS],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "ISOLATION_OK" in proc.stdout, f"stdout={proc.stdout}\nstderr={proc.stderr}"
