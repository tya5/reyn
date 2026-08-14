"""Tier 2: #4464 — image preparation runs off the event loop and shows a
"preparing" flowview entry, reusing the RUNNING-tool live-indicator
convention (no new visual vocabulary — the owner's explicit acceptance
criterion for this issue).

Owner's 3 requirements (quoted in #4464), each with its own test below:

1. Preparing an image must not stall other UI updates / conversation
   progress — :func:`test_image_decode_does_not_block_the_event_loop`.
2. The decode step runs via ``asyncio.to_thread`` (flowview's own
   ``FlowPresenter.present`` is already ``async`` and runs inside a Textual
   worker — the NEW piece #4464 adds is threading the CPU-heavy decode
   itself, not the already-async fetch) — the SAME test above witnesses
   this directly (a synchronous decode would block the loop; a threaded one
   does not), plus :func:`test_decoded_image_is_cached_after_resolution`
   confirms the decode actually ran and its result is cached, not merely
   that the loop stayed responsive for unrelated reasons.
3. A "preparing" flowview entry must be VISIBLE while resolving, reusing
   the existing ``_begin_running_indicator``/``_running_indicator``
   convention (``tests/interfaces/test_textual_chat_phase2b_live_tool_3283.py``'s
   own precedent) — :func:`test_presentation_entry_shows_the_running_indicator_while_resolving`
   and :func:`test_multi_image_entry_stays_preparing_until_every_image_settles`.

Real ``TextualChatApp`` + a real ``QueueTransport`` + a real ``ReynPresenter``
throughout (mirrors ``test_3846_image_resolution_e2e.py``'s own established
idiom) — ``httpx.AsyncClient`` is monkeypatched with a real-shaped stand-in
(pin_ssrf's loopback denial is unconditional, so there is no real reachable
collaborator to hit from CI), gated on a controllable ``asyncio.Event`` so
the test can inspect the IN-FLIGHT state before releasing the fetch.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat._meta_keys import RUNNING_SINCE_KEY
from reyn.interfaces.inline.textual_chat.presenter import ReynPresenter
from reyn.runtime.outbox import OutboxMessage
from tests._async_wait import wait_until
from tests._support.textual_chat_test_helpers import QueueTransport


def _png_bytes(size: tuple[int, int] = (32, 32)) -> bytes:
    import io

    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", size, color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class _StreamResp:
    def __init__(self, *, body: bytes, gate: "asyncio.Event | None" = None) -> None:
        self.headers = httpx.Headers({"content-type": "image/png"})
        self.status_code = 200
        self._body = body
        self._gate = gate

    def raise_for_status(self) -> None:
        pass

    async def aiter_bytes(self):  # noqa: ANN201
        if self._gate is not None:
            await self._gate.wait()
        yield self._body


class _StreamCtx:
    def __init__(self, resp: "_StreamResp") -> None:
        self._resp = resp

    async def __aenter__(self) -> _StreamResp:
        return self._resp

    async def __aexit__(self, *a: object) -> None:
        return None


def _client_factory(resp: "_StreamResp"):
    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        def stream(self, method: str, url: str) -> _StreamCtx:
            return _StreamCtx(resp)

    return _Client


def _presentation_frame(src: str, alt: str = "a photo") -> OutboxMessage:
    return OutboxMessage(
        kind="presentation",
        text="",
        meta={"nodes": [{"component": "image", "src": src, "alt": alt}]},
    )


async def _entry_text(app: TextualChatApp, entry: object) -> str:
    import io

    from rich.console import Console

    presentation = await app._presenter.present(entry, width=100)
    buf = io.StringIO()
    Console(file=buf, color_system=None, width=100).print(presentation.renderable)
    return buf.getvalue()


# ── 1+2: decode runs off the event loop ─────────────────────────────────────

class _FakeEntry:
    """The minimal surface `_resolve_image` calls on its `entry` param
    (`.update()`) — a real `textual_flowview.Entry` needs a live `FlowModel`
    to construct, which this presenter-only test has no reason to stand up
    (mirrors `begin_image_resolution`'s own module docstring: the presenter
    only ever calls `.update()` on it)."""

    def update(self) -> None:
        pass


@pytest.mark.asyncio
async def test_image_decode_does_not_block_the_event_loop(monkeypatch) -> None:
    """Tier 2: #4464 requirement 1+2 — a slow decode must not stall a
    concurrent coroutine. Patches ``decode_image_body`` to a genuinely
    CPU-blocking call (``time.sleep``, not ``asyncio.sleep`` — the real
    failure mode is synchronous CPU work on the loop, and only a real
    blocking call reproduces that); a concurrent ticking task must keep
    advancing THROUGHOUT the decode if — and only if — it actually runs on
    a background thread via ``asyncio.to_thread``.

    Falsifiable directly: reverting ``_resolve_image`` to call
    ``decode_image_body`` inline (no ``asyncio.to_thread``) would freeze
    the ticking task for the full sleep duration, and this test would fail
    (the tick count during the window would stay ~0 instead of climbing)."""
    monkeypatch.setattr(
        "reyn.interfaces.repl.present_renderer.decode_image_body",
        lambda body: (time.sleep(0.15) or object()),
    )
    presenter = ReynPresenter()
    ticks = 0
    stop = False

    async def _ticker() -> None:
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker_task = asyncio.create_task(_ticker())
    entry = _FakeEntry()
    try:
        monkeypatch.setattr(
            httpx, "AsyncClient", _client_factory(_StreamResp(body=_png_bytes()))
        )
        await presenter._resolve_image(entry, "https://example.com/x.png", None)
    finally:
        stop = True
        await ticker_task

    assert ticks >= 5, (
        f"only {ticks} event-loop ticks landed during a 150ms decode — "
        "the decode likely ran synchronously on the loop, blocking it"
    )
    assert presenter.has_decoded_image("https://example.com/x.png")


@pytest.mark.asyncio
async def test_decoded_image_is_cached_after_resolution(monkeypatch) -> None:
    """Tier 2: #4464 requirement 2's own positive witness — after a real
    fetch+decode settles, the decoded renderable is cached (public
    accessor, not private-state), so a later render can skip the decode
    entirely (`_render_image`'s own cache-hit branch, #4463's PR)."""
    monkeypatch.setattr(
        httpx, "AsyncClient", _client_factory(_StreamResp(body=_png_bytes()))
    )
    presenter = ReynPresenter()
    entry = _FakeEntry()
    src = "https://example.com/y.png"
    assert not presenter.has_decoded_image(src)
    await presenter._resolve_image(entry, src, None)
    assert presenter.has_decoded_image(src)


# ── 3: preparing entry is visible ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_presentation_entry_shows_the_running_indicator_while_resolving(
    monkeypatch,
) -> None:
    """Tier 2: #4464 requirement 3 — a presentation entry with an unresolved
    image carries the SAME `_RUNNING_SINCE_KEY` marker and renders the SAME
    `_running_indicator` line a RUNNING tool row already does (no new
    visual vocabulary) — then settles once resolution completes.

    Gates the fetch on a controllable `asyncio.Event` so the IN-FLIGHT
    state is inspected deterministically (no sleep-and-hope)."""
    gate = asyncio.Event()
    monkeypatch.setattr(
        httpx, "AsyncClient", _client_factory(_StreamResp(body=_png_bytes(), gate=gate))
    )
    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._ingest_frame(_presentation_frame("https://example.com/z.png"))
        await pilot.pause()
        entry = list(app.query_one(FlowView).entries)[-1]

        assert (entry.item.meta or {}).get(RUNNING_SINCE_KEY) is not None, (
            "an in-flight image resolution must stamp the same RUNNING_SINCE_KEY "
            "marker a RUNNING tool row uses"
        )
        text = await _entry_text(app, entry)
        assert "elapsed" in text, (
            f"expected the shared _running_indicator line ('elapsed Ns'), got: {text}"
        )

        gate.set()  # let the fetch complete
        await wait_until(lambda: (entry.item.meta or {}).get(RUNNING_SINCE_KEY) is None)
        settled_text = await _entry_text(app, entry)
        assert "elapsed" not in settled_text, (
            f"the running indicator must clear once resolution settles: {settled_text}"
        )


@pytest.mark.asyncio
async def test_multi_image_entry_stays_preparing_until_every_image_settles(
    monkeypatch,
) -> None:
    """Tier 2: #4464 — an entry with TWO images does not settle (drop the
    running indicator) until BOTH resolve, not just the first. Real
    behavior, not a declared intent: the fast image is a real httpx
    response that returns immediately; the slow one is gated on the SAME
    controllable event as the single-image test above."""
    gate = asyncio.Event()
    fast_body = _png_bytes((8, 8))
    slow_body = _png_bytes((16, 16))

    class _MultiClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_MultiClient":
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        def stream(self, method: str, url: str) -> _StreamCtx:
            if "slow" in url:
                return _StreamCtx(_StreamResp(body=slow_body, gate=gate))
            return _StreamCtx(_StreamResp(body=fast_body))

    monkeypatch.setattr(httpx, "AsyncClient", _MultiClient)
    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test() as pilot:
        await pilot.pause()
        msg = OutboxMessage(
            kind="presentation",
            text="",
            meta={"nodes": [
                {"component": "image", "src": "https://example.com/fast.png", "alt": "fast"},
                {"component": "image", "src": "https://example.com/slow.png", "alt": "slow"},
            ]},
        )
        app._ingest_frame(msg)
        await pilot.pause()
        entry = list(app.query_one(FlowView).entries)[-1]

        await wait_until(lambda: app._presenter.has_cached_image("https://example.com/fast.png"))
        # The fast image settled, but the slow one has not — the entry must
        # STILL be marked preparing (this is the real bug a naive
        # "strip on first settle" implementation would have).
        assert (entry.item.meta or {}).get(RUNNING_SINCE_KEY) is not None, (
            "entry settled after only ONE of two images resolved"
        )

        gate.set()
        await wait_until(lambda: (entry.item.meta or {}).get(RUNNING_SINCE_KEY) is None)
        presenter = app._presenter
        assert presenter.has_cached_image("https://example.com/slow.png")
