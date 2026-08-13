"""Tier 2: #3846 ①②③ — end-to-end image `src` resolution through the
REAL TextualChatApp -> ReynPresenter -> render_presentation_nodes chain.

Drives a real ``kind="presentation"`` frame with an ``image`` component node
through ``TextualChatApp`` (not a hand-authored unit call), confirming:
- the placeholder renders BEFORE resolution settles;
- the SAME entry re-renders with the loaded state AFTER the background
  fetch completes (proves the redraw-trigger, not just the cache write) —
  #3846 ③'s real-pixel rendering path (a real PNG body, not a magic-byte
  stub) is exercised here;
- a failed fetch renders a distinguishable failure state, not the same
  placeholder text a fetch that never started would show (the #3688 "two
  different things look the same" class).

``httpx.AsyncClient`` is monkeypatched with a real-shaped stand-in — the
SAME necessity as ``tests/core/test_3846_image_fetch.py`` (pin_ssrf=True's
loopback denial is unconditional, so there is no real reachable collaborator
to hit from CI).
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from textual_flowview import FlowView

from reyn.config.chat import ChatConfig
from reyn.config.root import ReynConfig
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.runtime.outbox import OutboxMessage
from tests._support.textual_chat_test_helpers import QueueTransport


class _StreamResp:
    def __init__(self, *, body: bytes, content_type: str = "image/png") -> None:
        self.headers = httpx.Headers({"content-type": content_type})
        self.status_code = 200
        self._body = body

    def raise_for_status(self) -> None:
        pass

    async def aiter_bytes(self):  # noqa: ANN201
        yield self._body


class _StreamCtx:
    def __init__(self, resp: "_StreamResp | Exception") -> None:
        self._resp = resp

    async def __aenter__(self) -> _StreamResp:
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp

    async def __aexit__(self, *a: object) -> None:
        return None


def _client_factory(resp: "_StreamResp | Exception"):
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


def _presentation_frame(src: str) -> OutboxMessage:
    return OutboxMessage(
        kind="presentation",
        text="",
        meta={"nodes": [{"component": "image", "src": src, "alt": "a cat"}]},
    )


async def _entry_text(app: TextualChatApp, entry: object) -> str:
    """Render `entry` through the app's REAL presenter — the same call
    ``FlowView`` itself makes to draw a row — and flatten to plain text.
    Reading `entry.item.text` (the raw OutboxMessage field) would NOT work
    here: a `presentation`-kind message's visible content comes from
    `meta["nodes"]` via `render_presentation_nodes`, computed at render
    time, not stored on `.text`."""
    import io

    from rich.console import Console

    presentation = await app._presenter.present(entry.item, width=100)
    buf = io.StringIO()
    Console(file=buf, color_system=None, width=100).print(presentation.renderable)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_placeholder_renders_before_resolution_settles(monkeypatch) -> None:
    """Tier 2: before the background fetch resolves, the entry shows the
    PLACEHOLDER text (the pre-#3846 shape), not a crash and not the loaded
    state prematurely."""
    monkeypatch.setattr(
        httpx, "AsyncClient", _client_factory(_StreamResp(body=b"\x89PNG-bytes"))
    )
    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._ingest_frame(_presentation_frame("https://example.com/cat.png"))
        # No pause yet — the fetch task is scheduled but hasn't had a chance
        # to run on the event loop.
        entry = list(app.query_one(FlowView).entries)[-1]
        text = await _entry_text(app, entry)
        assert "[image: a cat]" in text, text


@pytest.mark.asyncio
async def test_loaded_state_replaces_the_placeholder_after_resolution(monkeypatch) -> None:
    """Tier 2: ★ the redraw witness — after the background fetch settles, the
    entry's ``revision`` (flowview's own public, monotonic "was ``.update()``
    called" counter — bumped by NOTHING else in this test) has advanced, AND
    the SAME entry now renders the loaded (real-pixel, #3846 ③) state.

    ``revision`` is the load-bearing half: calling ``present()`` directly (as
    :func:`_entry_text` does, to read content) would show the loaded state
    from cache alone even if the redraw trigger were dead — a first version
    of this test asserted content only and stayed GREEN with
    ``entry.update()`` deleted from the implementation (caught by falsifying
    it directly). Content is still asserted too — a bumped revision with the
    WRONG content displayed would be an equally real bug.

    A REAL PNG body (not a bare magic-byte stub) is required here: ③ decodes
    the fetched body via PIL before rendering, so a fake body now degrades
    to the failure-to-render text — this test's own job is the SUCCESS path."""
    import io

    from PIL import Image as PILImage

    buf = io.BytesIO()
    # A reasonably-sized real PNG for the SUCCESS render path (see
    # present_renderer's own test file for the dedicated failure-path
    # tests — a corrupt/truncated body, exercised there instead).
    PILImage.new("RGB", (32, 32), color=(200, 20, 60)).save(buf, format="PNG")
    body = buf.getvalue()
    monkeypatch.setattr(
        httpx, "AsyncClient", _client_factory(_StreamResp(body=body, content_type="image/png"))
    )
    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._ingest_frame(_presentation_frame("https://example.com/cat.png"))
        entry = list(app.query_one(FlowView).entries)[-1]
        revision_before = entry.revision
        # Let the scheduled fetch task run to completion, then let the
        # entry.update() redraw materialize.
        await pilot.pause()
        await pilot.pause()
        assert entry.revision > revision_before, (
            "entry.revision never advanced — the redraw trigger did not fire"
        )
        text = await _entry_text(app, entry)
        # Exact half-block colour output is a pixel-level rendering detail
        # (see the module docstring rule against pinning that), so this
        # only asserts what's reyn's own to keep: the placeholder is gone,
        # no failure/decode-error text appeared, and SOMETHING non-empty
        # replaced it (the loaded state, not a crash swallowed to blank).
        assert "[image: a cat]" not in text, "the placeholder never cleared: " + text
        assert "could not render" not in text, text
        assert "[image failed" not in text, text
        assert text.strip(), "loaded state rendered empty output"


@pytest.mark.asyncio
async def test_a_failed_fetch_renders_a_distinguishable_failure_state(monkeypatch) -> None:
    """Tier 2: a fetch failure does NOT reuse the placeholder text (the
    #3688 "invisible/unreachable read as the same thing" class named in the
    #3846 design thread) — its own distinct failure state names the src."""
    monkeypatch.setattr(
        httpx, "AsyncClient", _client_factory(httpx.TimeoutException("timed out"))
    )
    app = TextualChatApp(transport=QueueTransport())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._ingest_frame(_presentation_frame("https://example.com/cat.png"))
        entry = list(app.query_one(FlowView).entries)[-1]
        await pilot.pause()
        await pilot.pause()
        text = await _entry_text(app, entry)
        assert "[image failed: a cat" in text, text
        assert "[image: a cat]" not in text, text
        assert "[image loaded:" not in text, text


@pytest.mark.asyncio
async def test_config_scheme_allowlist_reaches_the_fetch_call(monkeypatch) -> None:
    """Tier 2: `chat.image_url_schemes` (a REAL ReynConfig, not a stub) reaches
    the actual fetch call — an https-only allowlist rejects a plain-http src,
    surfacing as the failure state (proves the config value was actually
    threaded app -> presenter -> fetch, not merely stored)."""
    def _boom(**kwargs: Any):
        raise AssertionError("the allowlist gate must reject before any client call")
    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    config = ReynConfig(chat=ChatConfig(image_url_schemes=["https"]))
    app = TextualChatApp(transport=QueueTransport(), config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._ingest_frame(_presentation_frame("http://example.com/cat.png"))
        entry = list(app.query_one(FlowView).entries)[-1]
        await pilot.pause()
        await pilot.pause()
        text = await _entry_text(app, entry)
        assert "[image failed: a cat" in text, text
        assert "configured" in text.lower() or "scheme" in text.lower(), text
