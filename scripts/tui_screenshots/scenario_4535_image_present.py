"""#4535 scenario: the frame right after `present()` puts an image on screen.

Issue #4535 (owner, verbatim): 「★4535 は スクショで 確認したい」 — the question
is whether, right after an image is `present()`ed, (a) the bottom status bar
stays visible and (b) the image does not sandwich the composer between two
image bands.

This drives a REAL `TextualChatApp` through the REAL frame-ingestion pump
(`transport.push(...)` + `pilot.pause()` — the same path a live session's
frames arrive through, not the private `_ingest_frame` shortcut some tests
use) to the exact state: one prior user/agent exchange (so the chat surface
is not an empty first-turn screen), then a `kind="presentation"` frame whose
blueprint contains one `image` component, decoded and settled (not left in
the "preparing" spinner state) before the screenshot is taken.

The image `src` is `https://example.com/reyn-banner.png` — never actually
dialed: `httpx.AsyncClient` is monkeypatched to a real-shaped in-memory
stand-in serving REAL PNG bytes (generated via PIL, mirroring
`tests/interfaces/test_4464_image_prep_off_thread_and_indicator.py`'s own
established idiom), because this repo's `pin_ssrf` guard unconditionally
denies a real network fetch from this present-image path (#3846) and there
is no reachable real host to hit from CI/a sandboxed dev machine either way.
Only the NETWORK TRANSPORT is faked — the image bytes, decode, cache, and
render are all the real `ReynPresenter`/`present_renderer` code path.

CI: manual -- imported by tui_screenshot.py, never run directly.
"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import httpx

# #3024: this module imports `reyn` too (independent of whether the caller
# already guarded) -- see verify_env_identity.py's own guard_bare_script_or_
# exit docstring. `tui_screenshot.py` (the only sanctioned caller) already
# runs this before importing any scenario module, but this call stays here
# too: a scenario module is itself a `scripts/*.py`-shaped file per that
# guard's own contract, and this repeats at negligible cost (a single
# find_spec probe) rather than relying on caller discipline.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_REPO_ROOT / "src"), str(_REPO_ROOT), str(_REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from verify_env_identity import guard_bare_script_or_exit  # noqa: E402

guard_bare_script_or_exit()

from reyn.interfaces.inline.textual_chat import TextualChatApp  # noqa: E402
from reyn.interfaces.repl.read_model import (  # noqa: E402
    LOCAL_CHAT_READ_CAPABILITIES,
    ChatReadModel,
)
from reyn.runtime.outbox import OutboxMessage  # noqa: E402
from tests._support.textual_chat_test_helpers import QueueTransport  # noqa: E402

SIZE = (100, 30)

# A real-shaped status snapshot (same key set `interfaces/inline/app.py`'s own
# `_snapshot` produces — mirrors `test_textual_chat_phase4_3273.py`'s `_SNAP`
# fixture) so the status bar this scenario screenshots shows genuine Model /
# Agent / cost / ctx figures, not the read-model-absent zeros.
_SNAP = {
    "model": "claude-opus-4-8",
    "model_active_class": "opus",
    "model_classes": ["light", "opus", "strong"],
    "agent_names": ["default", "planner"],
    "attached_name": "default",
    "session_tree": [],
    "usage": (1200, 340, 1540),
    "cost_usd": 0.0123,
    "cost_agent": 0.0123,
    "cost_total": 0.0500,
    "agent_tokens": 1540,
    "ctx_used": 90000,
    "ctx_window": 200000,
    "ctx_source": "model",
    "ctx_recent_usage": (90000, 40000),
    "cache_usage_reported": True,
    "usage_breakdown_reported": True,
}


class _SnapshotReadModel(ChatReadModel):
    """A real :class:`ChatReadModel` seam impl returning a fixed real-shaped
    snapshot (mirrors `test_textual_chat_phase4_3273.py`'s own
    `_SnapshotReadModel`) — the status bar reads model/agent/cost/ctx off
    this same seam a live session's read model uses."""

    @property
    def capabilities(self):
        return LOCAL_CHAT_READ_CAPABILITIES

    def snapshot(self, config=None):
        return _SNAP

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    def clear_pending_command_ui(self) -> None:
        return None

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self) -> Path:
        return Path("/tmp/reyn_4535_screenshot_history")

    def conversation_history(self, *, limit=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


_IMAGE_SRC = "https://example.com/reyn-banner.png"


def _banner_png_bytes() -> bytes:
    """A real, decodable PNG (not a stub) — a wide banner shape (1200x300)
    matching what tui-coder's own real-machine #4535 check used for the
    "large/wide image" case, since a wide image is the shape most likely to
    visually sandwich the composer if it ever did."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1200, 300), color=(30, 60, 110))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, 1160, 260], outline=(240, 240, 240), width=6)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _StreamResp:
    def __init__(self, body: bytes) -> None:
        self.headers = httpx.Headers({"content-type": "image/png"})
        self.status_code = 200
        self._body = body

    def raise_for_status(self) -> None:
        pass

    async def aiter_bytes(self):
        yield self._body


class _StreamCtx:
    def __init__(self, resp: "_StreamResp") -> None:
        self._resp = resp

    async def __aenter__(self) -> "_StreamResp":
        return self._resp

    async def __aexit__(self, *a: object) -> None:
        return None


def _fake_async_client_factory(body: bytes):
    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        def stream(self, method: str, url: str) -> _StreamCtx:
            return _StreamCtx(_StreamResp(body))

    return _FakeClient


def make_app() -> TextualChatApp:
    # See the module docstring: only the network transport is faked — the
    # decode/cache/render path below is the real one `present()` uses.
    httpx.AsyncClient = _fake_async_client_factory(_banner_png_bytes())
    return TextualChatApp(
        transport=QueueTransport(), read_model=_SnapshotReadModel()
    )


def _presentation_frame(src: str, alt: str) -> OutboxMessage:
    return OutboxMessage(
        kind="presentation",
        text="",
        meta={"nodes": [{"component": "image", "src": src, "alt": alt}]},
    )


async def direct(app: TextualChatApp, pilot) -> None:
    transport: QueueTransport = app._transport  # type: ignore[assignment]

    # #4535's own concern (bottom status bar / composer / image) is
    # independent of the sent-queue gate's own STATE_SNAPSHOT seeding
    # (`tests/_support/textual_chat_test_helpers.QueueTransport` only ever
    # wraps pushed items as DisplayFrame, so it cannot carry a
    # `StatusApplied(kind="snapshot")` seed the way the sent-queue-specific
    # `test_3300_p2b_sentqueue_render.py`'s own transport double can). The
    # status bar's Model/Agent/cost/ctx figures come from `read_model.
    # snapshot()` (`make_app`'s `_SnapshotReadModel`), not from this
    # transport stream, so this scenario is unaffected by that gap — only
    # a benign "frame arrived before any STATE_SNAPSHOT" log line results.

    # One prior exchange, so the screenshot shows the chat surface actually
    # in use (an empty first-turn screen would not exercise the composer/
    # status-bar layout #4535 is asking about).
    await transport.push(OutboxMessage(kind="user", text="show me the new banner"))
    await pilot.pause()
    await transport.push(
        OutboxMessage(kind="agent", text="here it is:", meta={"chain_id": "c1"})
    )
    await pilot.pause()

    # The frame under test: an image `present()`ed immediately after.
    await transport.push(_presentation_frame(_IMAGE_SRC, alt="reyn banner"))
    await pilot.pause()

    # Wait for the real background decode to settle (#4464's own
    # `has_decoded_image` accessor) — never a fixed sleep. Unbounded: a
    # decode that never settles should hang this script visibly, not pass
    # silently on a guessed timeout.
    while not app._presenter.has_decoded_image(_IMAGE_SRC):  # noqa: SLF001
        await asyncio.sleep(0.01)
        await pilot.pause()

    # One more paint so the settled (non-"preparing") body is what renders.
    await pilot.pause()
