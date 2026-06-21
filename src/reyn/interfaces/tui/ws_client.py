"""WebSocket client adapter for ``reyn chat --connect``.

Issue #276 Phase A — proof-of-concept WS thin client. Connects to a
running ``reyn web`` server's ``/ws/chat/{agent_name}`` endpoint and
exposes:

- :class:`_WSRegistry` — a minimal AgentRegistry shape (= just enough
  attributes for ``ReynTUIApp._outbox_loop`` and ``_get_session`` to
  function). Drains the WS connection in a background task and pushes
  reconstructed ``OutboxMessage`` items onto its ``repl_outbox`` queue.
- :class:`_WSSessionProxy` — a minimal Session shape exposing the
  TUI's submit / session-attribute paths the foreground code touches
  on user input. Phase B will expand this with intervention answer /
  cancel / list-agents support.

Phase A scope is intentionally tight: chat round-trip (= user submit
→ remote turn → frames stream back) works; right-panel features that
read local files (events / memory / agents / cost / docs) show
empty / "remote — limited" placeholders via the existing
``--connect`` integration on each tab (e.g. Pending tab's
``remote_mode`` from issue #277).

Server-side WS protocol is the existing ``src/reyn/web/ws/chat.py``
endpoint — no server changes required for Phase A.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from urllib.parse import urljoin

from reyn.runtime.outbox import OutboxMessage

logger = logging.getLogger(__name__)


# Server forwards these kinds (= matches ``_FORWARDED_KINDS`` in
# ``src/reyn/web/ws/chat.py``). Plus the keepalive synthetic status.
# The TUI's OutboxRouter already dispatches on these.
_KNOWN_KINDS = frozenset({
    "agent", "status", "error", "intervention", "trace", "skill_done",
    # Issue #276 Phase B (4/5): ``__attach_request__`` is now
    # forwarded so the TUI's ``_on_attach_request`` handler can
    # update the header label + clear the conv pane when a remote
    # ``/attach`` triggers the server-side swap.
    "__attach_request__",
    # ``intervention_resolved`` was always forwarded by the server
    # via the existing ``status`` / ``intervention`` pipeline — listed
    # here for explicitness so the parse-time `unknown kind`
    # diagnostic doesn't fire on it.
    "intervention_resolved",
})


class _WSSessionProxy:
    """Minimal Session-shaped object backing the TUI in ``--connect`` mode.

    The TUI reads ``agent_name`` (for header / Pending tab claim
    channel id construction) and calls ``submit_user_text(text)`` on
    user input. Everything else is intentionally absent for Phase A
    — accesses fall through to ``None`` / ``AttributeError`` which
    the TUI already handles defensively (Pending tab, intervention
    queued_count, etc. show empty / placeholder).

    Phase B will widen this proxy: ``running_skills`` / ``_interventions`` /
    ``_maybe_answer_oldest_intervention`` / ``_registry`` / etc., each
    backed by an additional client→server message type that the
    server-side WS endpoint will need to gain.
    """

    def __init__(self, agent_name: str, send_fn) -> None:
        # ``send_fn`` is an async callable that sends a JSON-encoded
        # client→server message. Owned by the registry — passed in
        # so the proxy doesn't need a reference to the WebSocket.
        self.agent_name = agent_name
        self._send_fn = send_fn
        # The TUI inspects these attributes via ``getattr(..., None)``
        # in several paths. Setting them to None keeps Phase A read
        # surfaces from raising — they each render the
        # "limited / remote" placeholder downstream.
        self._interventions = None
        self.running_skills: dict = {}

    @property
    def interventions(self):
        """Intervention registry — ``None`` in Phase A proxy (no local state).

        Local ``Session`` stores intervention data in ``_interventions``.
        The Phase A proxy has no round-trip to the server for this, so
        ``None`` is the correct placeholder: TUI paths that read it already
        use ``getattr(session, "_interventions", None)`` / ``getattr(session,
        "interventions", None)`` so this keeps the API shape compatible.
        Phase B may grow a server-side intervention sync path.

        Exposed via the public name so tests can assert the Phase A default
        without accessing the private ``_interventions`` attribute directly
        (per feedback_test_public_surface_not_private_state policy).
        """
        return self._interventions

    async def submit_user_text(self, text: str) -> None:
        """Send a ``user_message`` WS frame.

        Mirrors :py:meth:`Session.submit_user_text` from the TUI's
        point of view — kicks off a turn on the remote agent.
        """
        await self._send_fn({"type": "user_message", "text": text})

    def register_intervention_listener(self, listener_id: str) -> None:
        """No-op stub for the TUI's ``on_mount`` listener registration.

        The local ``Session`` uses this to declare which UI surface
        consumes intervention prompts (= local TUI vs MCP vs A2A). In
        remote (``--connect``) mode the server-side session handles its
        own listener registration; the thin client doesn't need to
        coordinate. Recording the listener id keeps the API shape
        compatible without adding a round-trip to the server.

        Phase A omitted this method which crashed
        ``ReynTUIApp.on_mount`` on first ``--connect`` launch (= this
        was a Phase A regression caught during Phase B integration
        testing). Phase B adds it as a no-op so the proxy can be
        mounted; future phases may grow a server-side listener
        registration if needed.
        """
        self._intervention_listener_id = listener_id

    async def _maybe_handle_slash(self, text: str) -> bool:
        """Issue #276 Phase B (4/5): forward all slash commands to the server.

        The local ``Session._maybe_handle_slash`` dispatches to a
        registered handler (e.g. ``agents_cmd``, ``attach_cmd``) that
        reads ``session._registry`` — server-side state the proxy
        cannot reach locally. Forward the raw command text instead;
        the server runs the slash handler in its own session context
        and the resulting ``system`` / ``error`` / ``__attach_request__``
        outbox frames flow back via the existing forwarding pipeline.

        Without this method ``ReynTUIApp.on_input_bar_user_submitted``
        crashed with ``AttributeError`` on any slash command in
        ``--connect`` mode (= Phase A regression caught during
        Phase B testing).

        Returns ``True`` to match the local API shape (= "slash
        handled, don't fall through to user_message"). Actual
        execution is async server-side; the visible result arrives
        as the next inbound frame.
        """
        await self._send_fn({"type": "slash_command", "text": text})
        return True

    async def _maybe_answer_oldest_intervention(self, text: str) -> None:
        """Issue #276 Phase B (2/5): send an ``answer_intervention`` WS frame.

        Mirrors :py:meth:`Session._maybe_answer_oldest_intervention`
        from the TUI's intervention-widget callback path: when the
        user clicks a chip or types a free-text answer, the widget's
        ``answer_callback`` calls this with the chosen string.
        Server-side handler resolves the head intervention + delivers
        the answer via the existing
        ``InterventionHandler.maybe_answer`` path; the matching
        choice-vs-text dispatch is identical to local mode. Status
        flows back as the next outbox frame.

        Without this method the TUI's ``_mount_intervention``
        callback raised ``AttributeError`` in remote mode when the
        user clicked a chip or submitted a free-text answer (= Phase
        A regression caught during Phase B answer testing). Returns
        ``None`` — authoritative outcome arrives as a server-side
        outbox frame.
        """
        await self._send_fn({"type": "answer_intervention", "text": text})

    async def cancel_inflight(self) -> None:
        """Issue #276 Phase B: send a ``cancel_inflight`` WS frame.

        The proxy holds no real ``running_skills``
        — that state lives on the server. The TUI delegates Ctrl+C to
        this method when ``--connect`` mode is detected, and the
        server-side endpoint (= ``src/reyn/web/ws/chat.py``) iterates
        its session's running tasks + emits a ``status`` outbox with
        the same "✗ cancelled N skill + M plan" summary local mode
        produces.

        Returns immediately after the WS send — actual cancellation
        is async on the server, the visible result arrives via the
        next inbound ``status`` frame.
        """
        await self._send_fn({"type": "cancel_inflight"})


class _WSRegistry:
    """Minimal AgentRegistry-shaped object backing the TUI in ``--connect`` mode.

    The TUI's outbox loop reads from ``self.repl_outbox`` (an
    asyncio.Queue of OutboxMessage). We populate that queue from
    WS receive frames in :meth:`_receive_loop`. The TUI's
    ``_get_session`` calls :meth:`attached_session`; we return the
    cached :class:`_WSSessionProxy`.

    ``_project_root`` is set so existing TUI paths that read it (e.g.
    Right Panel tab data sources) don't crash — they each have their
    own remote-mode handling that surfaces empty / placeholder
    content because the underlying data lives on the server.
    """

    def __init__(
        self,
        agent_name: str,
        websocket,
        *,
        project_root,
    ) -> None:
        self._agent_name = agent_name
        self._ws = websocket
        self._project_root = project_root
        self.repl_outbox: asyncio.Queue = asyncio.Queue()
        self._receive_task: asyncio.Task | None = None
        # Build the session proxy — pass send_fn that goes through the
        # registry-owned websocket.
        self._session = _WSSessionProxy(
            agent_name=agent_name, send_fn=self._send,
        )

    async def _send(self, payload: dict) -> None:
        """Send a JSON-encoded message to the server."""
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            logger.warning("ws_client: send failed: %s", exc)

    def attached_session(self):
        """Return the WS-backed session proxy (= used by ``app._get_session``)."""
        return self._session

    def start(self) -> None:
        """Kick off the background WS receive loop."""
        if self._receive_task is None:
            self._receive_task = asyncio.create_task(self._receive_loop())

    async def shutdown(self) -> None:
        """Cancel the receive loop + close the WS — called on TUI exit."""
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self._ws.close()
        except Exception:
            pass
        # Wake up the outbox loop one last time so it can break out.
        await self.repl_outbox.put(OutboxMessage(kind="__end__", text=""))

    async def _receive_loop(self) -> None:
        """Drain the WS, parse each frame into OutboxMessage, enqueue."""
        try:
            async for raw in self._ws:
                msg = _parse_frame(raw)
                if msg is None:
                    continue
                await self.repl_outbox.put(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Surface a single error frame so the user can see why the
            # stream stopped (= not silent freeze).
            logger.warning("ws_client: receive loop ended: %s", exc)
            await self.repl_outbox.put(OutboxMessage(
                kind="error",
                text=f"connection lost: {exc}",
                # Wave-13 T1-3: sentinel lets app_outbox._on_error distinguish
                # a WS disconnection from a normal server-side error frame, so
                # the TUI can surface a persistent sticky and disable InputBar
                # (= the session is unrecoverable without restart).
                meta={"source": "ws_disconnected"},
            ))


def _parse_frame(raw) -> OutboxMessage | None:
    """Reconstruct an :class:`OutboxMessage` from a server WS frame.

    Server frames are JSON ``{"kind", "text", "meta"}`` per
    ``src/reyn/web/ws/chat.py``. Keepalive pings carry ``meta.$keepalive``
    and ``text=""``; we drop those (= no user-visible effect).
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("ws_client: malformed frame: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("kind", ""))
    text = str(payload.get("text", ""))
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    # Drop keepalive pings — the WS endpoint sends them every 30 s
    # when no real outbox traffic flows, marked via ``meta.$keepalive``.
    if meta.get("$keepalive"):
        return None
    # Pass through unknown kinds too — the TUI's OutboxRouter default
    # branch will render them via plain Rich text. Future server-side
    # kinds (= mcp_progress etc.) flow naturally.
    if kind not in _KNOWN_KINDS:
        logger.debug("ws_client: forwarding unknown kind %r", kind)
    return OutboxMessage(kind=kind, text=text, meta=meta)


def _build_ws_url(base: str, agent_name: str) -> str:
    """Convert ``ws://host[:port][/]`` + agent name → full ``/ws/chat/<agent>`` URL.

    Accepts a host-style base (= ``ws://localhost:8080``) plus the
    positional ``agent_name`` from the CLI. The user supplies the
    endpoint host; the WS path is internal.
    """
    if not base.endswith("/"):
        base = base + "/"
    return urljoin(base, f"ws/chat/{agent_name}")


async def connect(
    base_url: str, agent_name: str, *, project_root,
) -> _WSRegistry:
    """Open a WS connection to a running ``reyn web`` server.

    Raises :class:`ImportError` with a friendly message when the
    ``websockets`` package isn't installed (= ``pip install reyn[web]``).
    Raises :class:`ConnectionError` with the underlying cause on
    failed handshake / unreachable host so the CLI can print a
    one-line diagnostic instead of a stack trace.
    """
    try:
        import websockets  # noqa: F401
        from websockets.asyncio.client import connect as ws_connect
    except ImportError as exc:
        raise ImportError(
            "reyn chat --connect requires the 'websockets' package; "
            "install with: pip install -e '.[web]'"
        ) from exc

    url = _build_ws_url(base_url, agent_name)
    try:
        ws = await ws_connect(url)
    except Exception as exc:
        raise ConnectionError(
            f"failed to connect to {url}: {exc}"
        ) from exc
    reg = _WSRegistry(agent_name, ws, project_root=project_root)
    reg.start()
    print(
        f"connected to {url}",
        file=sys.stderr,
    )
    return reg


__all__ = [
    "connect",
    "_WSRegistry",
    "_WSSessionProxy",
    "_parse_frame",
    "_build_ws_url",
]
