"""Tier 2: #3846 ③ — `run_textual_chat` triggers `textual_image.renderable`'s
import BEFORE `app.run_async()`, not lazily during the live session.

`textual_image.renderable`'s terminal-capability auto-detection
(Sixel/TGP query) is a one-time, process-global, module-import-time side
effect that reads raw stdin with a synchronous escape-sequence round-trip.
The library's OWN docstring
(`textual_image.renderable.sixel.query_terminal_support`) warns this "will
not work anymore once Textual is started" — Textual's own stdin-reading
thread races (and usually wins) that read once the app loop is live.
`present_renderer.py`'s `_render_image` imports the SAME module lazily (its
own "No I/O" invariant), so if `run_textual_chat` did not ALSO trigger the
import eagerly, the first image ever presented would trigger it for the
first time DURING a live `app.run_async()` session — guaranteeing the
auto-detection loses the race and every image falls back to unicode/
half-block even on a real Kitty/WezTerm/Sixel terminal.

This only tests reyn's own ORDERING contract (the import statement runs
before `TextualChatApp.run_async` is awaited) — not the underlying terminal
query's own behavior, network reachability, or actual pixel output (all
third-party/environment properties, not reyn's to test)."""
from __future__ import annotations

import sys

import pytest


@pytest.mark.asyncio
async def test_textual_image_renderable_is_imported_before_app_run_async(monkeypatch) -> None:
    """Tier 2: falsify-verified — moving the eager import to AFTER
    `app.run_async(...)` in `run_textual_chat` makes this go RED (confirmed
    by temporarily reordering the two statements during development)."""
    import reyn.interfaces.inline.textual_chat.app as app_module

    # Force a real "not yet imported" starting state so this test observes
    # a genuine before/after transition, not a false pass from an earlier
    # test (or import elsewhere in this process) having already cached it.
    sys.modules.pop("textual_image.renderable", None)
    sys.modules.pop("textual_image", None)

    seen_before_run_async: list[bool] = []

    class _FakeApp:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def run_async(self, *, inline: bool) -> None:
            seen_before_run_async.append("textual_image.renderable" in sys.modules)

    monkeypatch.setattr(app_module, "TextualChatApp", _FakeApp)

    from tests._support.textual_chat_test_helpers import QueueTransport

    try:
        await app_module.run_textual_chat(transport=QueueTransport())
    finally:
        sys.modules.pop("textual_image.renderable", None)
        sys.modules.pop("textual_image", None)

    assert seen_before_run_async == [True], (
        "textual_image.renderable was not imported before TextualChatApp."
        "run_async() was called — the terminal-capability query would race "
        "Textual's own stdin reader and lose"
    )
