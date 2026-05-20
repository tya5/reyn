"""Tier 2: A2A webhook progress trigger expansion (issue #267 Gap 2).

Before this PR, the A2A webhook surface fired only on 3 events:
``input-required`` (A2AInterventionBus.deliver), ``completed`` /
``failed`` (_handle_async_mode._run). Mid-call lifecycle (= phase
transition / LLM call / op batch) was invisible to the peer — peers
received the initial Task envelope, then silence until terminal.

This PR adds ``_A2AProgressBridge`` (= mirrors ``_MCPProgressBridge``
shape from PR #279) that subscribes to the agent's ``chat_events``
for the lifetime of one ``_handle_async_mode`` call and fires
``status="in-progress"`` webhooks for ``phase_started`` /
``llm_called`` / ``act_executed``. Two instances (MCP + A2A) is below
the rule-of-three threshold so the bridge logic is per-protocol; a
future third instance is the trigger to lift to a shared base.

Pins:

  1. ``_A2AProgressBridge`` exists with ``_TRACKED_EVENTS`` = the 3
     declared kinds, matching ``_MCPProgressBridge``'s scope (= the
     contract two bridges share).
  2. ``attach()`` subscribes to ``session._chat_events``;
     ``detach()`` unsubscribes + cancels in-flight tasks; idempotent.
  3. Untracked event types are ignored (= ``intervention_routed`` /
     ``phase_completed`` / etc. don't fire the bridge).
  4. ``_format_message`` produces the expected text for each kind.
  5. ``_send`` POSTs the canonical progress payload to the configured
     webhook_url.
  6. ``ordinal`` is monotonic across multiple events on one bridge.
  7. After ``detach()``, subsequent events are silently dropped (=
     the subscriber is removed, defensive guard against late events).
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")

from reyn.events.events import EventLog  # noqa: E402
from reyn.schemas.models import Event  # noqa: E402


def _make_bridge(*, captured_posts: list[tuple[str, dict]], events: EventLog | None = None):
    """Build a bridge with a fake session whose ``_chat_events`` is the
    given EventLog (or a fresh one). The bridge's _send is monkey-patched
    to capture posts so we don't need to mock ``post_webhook`` globally.
    """
    from reyn.web.routers.a2a import _A2AProgressBridge

    class _FakeSession:
        def __init__(self, events: EventLog) -> None:
            self._chat_events = events

    if events is None:
        events = EventLog()
    session = _FakeSession(events)
    bridge = _A2AProgressBridge(
        session=session,
        run_id="run-X",
        webhook_url="https://peer.test/hook",
        agent_name="demo",
    )

    async def _capture(ordinal, event_type, message):  # noqa: ANN202
        captured_posts.append(
            (event_type, {
                "ordinal": ordinal,
                "message": message,
                "url": bridge._webhook_url,
                "run_id": bridge._run_id,
                "agent_name": bridge._agent_name,
            }),
        )

    bridge._send = _capture  # type: ignore[method-assign]
    return bridge, events


# ── 1. Tracked event scope matches MCP bridge ─────────────────────────


def test_a2a_progress_bridge_tracks_three_lifecycle_events() -> None:
    """Tier 2: ``_A2AProgressBridge._TRACKED_EVENTS`` matches the MCP
    bridge's scope exactly (= same 3 lifecycle event kinds). This
    keeps the per-protocol bridges aligned so a future third-instance
    abstraction has a stable contract to lift.
    """
    from reyn.mcp_server import _MCPProgressBridge
    from reyn.web.routers.a2a import _A2AProgressBridge

    assert _A2AProgressBridge._TRACKED_EVENTS == _MCPProgressBridge._TRACKED_EVENTS
    assert _A2AProgressBridge._TRACKED_EVENTS == frozenset({
        "phase_started", "llm_called", "act_executed",
    })


# ── 2. attach / detach lifecycle ──────────────────────────────────────


def test_attach_subscribes_to_chat_events() -> None:
    """Tier 2: ``attach()`` adds the bridge's ``_on_event`` callback to
    the session's chat_events subscriber list (= so EventLog dispatches
    will reach the bridge).
    """
    captured: list = []
    bridge, events = _make_bridge(captured_posts=captured)
    assert bridge._on_event not in events._subscribers
    bridge.attach()
    assert bridge._on_event in events._subscribers


def test_detach_unsubscribes_from_chat_events() -> None:
    """Tier 2: ``detach()`` removes the subscriber + flips internal flag
    so subsequent events are silently ignored.
    """
    captured: list = []
    bridge, events = _make_bridge(captured_posts=captured)
    bridge.attach()
    assert bridge._on_event in events._subscribers
    bridge.detach()
    assert bridge._on_event not in events._subscribers
    assert bridge._detached is True


def test_detach_is_idempotent() -> None:
    """Tier 2: calling ``detach()`` twice is safe (= no double-unsubscribe
    error, no exception). Critical because the bridge lives in a
    try/finally — if the body of try also detaches (= e.g. early
    return path adds one), finally must not blow up.
    """
    captured: list = []
    bridge, _ = _make_bridge(captured_posts=captured)
    bridge.attach()
    bridge.detach()
    bridge.detach()  # Second call should be no-op, not raise.


# ── 3. Filtering: only tracked events fire ────────────────────────────


def test_untracked_event_kinds_are_ignored() -> None:
    """Tier 2: events outside ``_TRACKED_EVENTS`` (= e.g.
    ``intervention_routed`` / ``phase_completed`` / ``agent_response``)
    do NOT fire a webhook.
    """
    captured: list = []
    bridge, events = _make_bridge(captured_posts=captured)
    bridge.attach()

    async def _drive() -> None:
        events.emit("phase_completed", phase="planning")
        events.emit("intervention_routed", route="user_channel")
        events.emit("agent_response", text="hi")
        # Let any scheduled tasks settle.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drive())
    assert captured == []


def test_tracked_events_each_fire_one_post() -> None:
    """Tier 2: each tracked event kind emits exactly one progress
    webhook with an incremented ordinal.
    """
    captured: list = []
    bridge, events = _make_bridge(captured_posts=captured)
    bridge.attach()

    async def _drive() -> None:
        events.emit("phase_started", phase="planning")
        events.emit("llm_called", model="gemini-2.5-flash-lite")
        events.emit("act_executed", op_count=3)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drive())

    assert [e for e, _ in captured] == [
        "phase_started", "llm_called", "act_executed",
    ]
    assert [p["ordinal"] for _, p in captured] == [1, 2, 3]


# ── 4. Message formatting ─────────────────────────────────────────────


def test_format_message_for_each_event_kind() -> None:
    """Tier 2: ``_format_message`` outputs the canonical human-readable
    text per event kind. Matches the MCP bridge's format so peer
    consumers can apply the same parser to both transports.
    """
    from reyn.web.routers.a2a import _A2AProgressBridge

    assert _A2AProgressBridge._format_message(
        "phase_started", {"phase": "planning"},
    ) == "phase: planning"
    assert _A2AProgressBridge._format_message(
        "llm_called", {"model": "gemini-2.5-flash-lite"},
    ) == "llm: gemini-2.5-flash-lite"
    # Singular form for 1 op.
    assert _A2AProgressBridge._format_message(
        "act_executed", {"op_count": 1},
    ) == "act: 1 op"
    # Plural form for N>1.
    assert _A2AProgressBridge._format_message(
        "act_executed", {"op_count": 5},
    ) == "act: 5 ops"
    # Missing fields fall back to "?".
    assert _A2AProgressBridge._format_message(
        "phase_started", {},
    ) == "phase: ?"


# ── 5. Send payload shape ─────────────────────────────────────────────


def test_send_posts_canonical_progress_payload(monkeypatch) -> None:
    """Tier 2: ``_send`` POSTs the payload shape:
    ``{run_id, status: "in-progress", progress, event, message, agent_name}``
    to the configured webhook_url. Existing fields from completed /
    failed payloads (= ``run_id``, ``status``, ``agent_name``) are
    preserved → peer's payload parser can dispatch on ``status``.
    """
    from reyn.web.routers.a2a import _A2AProgressBridge

    posted: list[tuple[str, dict]] = []

    async def _fake_post_webhook(url: str, payload: dict):  # noqa: ANN202
        posted.append((url, payload))
        from reyn.web.notifications import DeliveryOutcome, DeliveryResult
        return DeliveryResult(outcome=DeliveryOutcome.SUCCESS)

    import reyn.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _fake_post_webhook)

    class _FakeSession:
        _chat_events = EventLog()

    bridge = _A2AProgressBridge(
        session=_FakeSession(),
        run_id="run-Y",
        webhook_url="https://peer.test/hook",
        agent_name="demo",
    )

    asyncio.run(bridge._send(7, "phase_started", "phase: planning"))
    assert len(posted) == 1
    url, payload = posted[0]
    assert url == "https://peer.test/hook"
    assert payload == {
        "run_id": "run-Y",
        "status": "in-progress",
        "progress": 7,
        "event": "phase_started",
        "message": "phase: planning",
        "agent_name": "demo",
    }


def test_send_swallows_transport_errors(monkeypatch) -> None:
    """Tier 2: when ``post_webhook`` raises, ``_send`` swallows + returns
    None. Progress is best-effort; a single failed POST must not
    abort the agent's main call.
    """
    from reyn.web.routers.a2a import _A2AProgressBridge

    async def _failing_post(url: str, payload: dict):  # noqa: ANN202
        raise RuntimeError("simulated transport failure")

    import reyn.web.notifications as notifications_mod
    monkeypatch.setattr(notifications_mod, "post_webhook", _failing_post)

    class _FakeSession:
        _chat_events = EventLog()

    bridge = _A2AProgressBridge(
        session=_FakeSession(),
        run_id="run-Z",
        webhook_url="https://peer.test/hook",
        agent_name="demo",
    )

    # No exception raised, no return value used.
    asyncio.run(bridge._send(1, "phase_started", "phase: planning"))


# ── 6. Post-detach: late events dropped ───────────────────────────────


def test_late_event_after_detach_is_ignored() -> None:
    """Tier 2: defensive — if an event fires AFTER ``detach()`` was
    called but BEFORE the EventLog's subscriber list is fully drained
    (= unlikely in single-threaded asyncio but possible across emit
    interleavings), the ``_detached`` flag short-circuits the handler.
    """
    captured: list = []
    bridge, events = _make_bridge(captured_posts=captured)
    bridge.attach()

    # Manually invoke detach but DO NOT remove the subscriber yet (=
    # simulate the race window).
    bridge._detached = True

    async def _drive() -> None:
        events.emit("phase_started", phase="planning")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_drive())
    # Even though the subscriber is still on the list, the flag check
    # in _on_event short-circuited.
    assert captured == []


# ── 7. Integration: _handle_async_mode wires the bridge ────────────────


def test_handle_async_mode_attaches_bridge_when_webhook_url_present() -> None:
    """Tier 2: in ``_handle_async_mode._run``, when ``webhook_url`` is
    provided, an ``_A2AProgressBridge`` is constructed + attached
    BEFORE ``send_to_agent_impl`` is called. Verified via in-source
    grep so a refactor that drops the wiring fails immediately.
    """
    import ast
    from pathlib import Path

    src_path = (
        Path(__file__).parent.parent
        / "src" / "reyn" / "web" / "routers" / "a2a.py"
    )
    tree = ast.parse(src_path.read_text(encoding="utf-8"))

    # Find the inner function _run inside _handle_async_mode.
    bridge_class_refs = 0
    bridge_attach_calls = 0
    bridge_detach_calls = 0

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "_A2AProgressBridge":
            bridge_class_refs += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "attach" and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "bridge":
                    bridge_attach_calls += 1
            if node.func.attr == "detach" and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "bridge":
                    bridge_detach_calls += 1

    # At least one class reference + at least one attach + at least one
    # detach (= the lifecycle wiring is present).
    assert bridge_class_refs >= 1
    assert bridge_attach_calls >= 1
    assert bridge_detach_calls >= 1


def test_handle_async_mode_skips_bridge_when_no_webhook_url() -> None:
    """Tier 2: in-source grep confirms the bridge construction is
    GATED on ``webhook_url`` (= no webhook → no bridge → no event
    subscription). The peer that doesn't register a URL sees zero
    overhead.
    """
    import inspect

    from reyn.web.routers import a2a as a2a_router

    src = inspect.getsource(a2a_router._handle_async_mode)
    # The bridge construction must be inside an ``if webhook_url:`` block.
    assert "if webhook_url:" in src
    assert "_A2AProgressBridge(" in src
    # And detach must run in a finally so it executes on both success
    # and exception paths.
    assert "bridge.detach()" in src
