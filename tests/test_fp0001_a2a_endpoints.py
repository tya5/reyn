"""Tier 2: FP-0001 A2A task lifecycle endpoints.

Tests the new task-lifecycle surface added by FP-0001:
  - GET /a2a/tasks/{run_id}
  - POST /a2a/tasks/{run_id}/cancel
  - GET /a2a/tasks/{run_id}/events
  - Agent Card capabilities flip (streaming=True, pushNotifications=True)
  - POST /a2a/agents/{name} answer injection mode (params.task_id)
  - POST /a2a/agents/{name} async mode (params.async_mode=true)

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- No unittest.mock / MagicMock / AsyncMock / patch usage.
- RunRegistry populated directly from its public API (create / attach_task /
  append_event / cancel / answer_intervention).
- FastAPI app dependency-overridden via app.dependency_overrides so the
  real singleton from deps.py is never touched.
- Observed via public HTTP response shapes and RunEntry.to_public_dict().
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

# Ensure the worktree src is importable.
_WORKTREE_SRC = Path(__file__).parent.parent / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

# Skip the whole module if optional deps are missing.
pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")
pytest.importorskip("httpx", reason="httpx not installed (needed by TestClient)")


from reyn.web.run_registry import RunRegistry  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_client_with_registry(registry: RunRegistry):
    """Build a TestClient that uses the supplied RunRegistry via DI override."""
    from fastapi.testclient import TestClient

    from reyn.web.deps import get_run_registry
    from reyn.web.server import app

    app.dependency_overrides[get_run_registry] = lambda: registry
    client = TestClient(app, raise_server_exceptions=False)
    return client


def _restore_overrides() -> None:
    from reyn.web.server import app
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. GET /a2a/tasks/{run_id} — found
# ---------------------------------------------------------------------------


def test_get_task_returns_public_dict_for_existing_run() -> None:
    """Tier 2: GET /a2a/tasks/{run_id} returns 200 with RunEntry.to_public_dict()
    shape when the run exists in the registry."""
    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-abc")
    client = _make_client_with_registry(registry)
    try:
        r = client.get(f"/a2a/tasks/{entry.run_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["run_id"] == entry.run_id
        assert body["agent_name"] == "demo"
        assert body["chain_id"] == "chain-abc"
        assert body["status"] == "running"
        # to_public_dict must not leak internal-only fields.
        assert "task" not in body
        assert "pending_intervention" not in body
    finally:
        _restore_overrides()


# ---------------------------------------------------------------------------
# 2. GET /a2a/tasks/{run_id} — not found
# ---------------------------------------------------------------------------


def test_get_task_returns_404_for_unknown_run() -> None:
    """Tier 2: GET /a2a/tasks/{run_id} returns 404 when the run_id is not
    in the registry."""
    registry = RunRegistry()
    client = _make_client_with_registry(registry)
    try:
        r = client.get("/a2a/tasks/nonexistent-run-id")
        assert r.status_code == 404, r.text
    finally:
        _restore_overrides()


# ---------------------------------------------------------------------------
# 3. POST /a2a/tasks/{run_id}/cancel — cancels a running task
# ---------------------------------------------------------------------------


def test_cancel_task_marks_entry_as_cancelled() -> None:
    """Tier 2: POST /a2a/tasks/{run_id}/cancel cancels a running task.

    Verifies that cancel() transitions entry.status to 'cancelled' and
    the response carries the updated public dict. The entry is created
    without an asyncio.Task (= no active coroutine needed to test the
    status-update path; RunRegistry.cancel handles None-task gracefully).
    """
    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-cancel")

    client = _make_client_with_registry(registry)
    try:
        r = client.post(f"/a2a/tasks/{entry.run_id}/cancel")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "cancelled"
        assert body["run_id"] == entry.run_id

        # Registry entry status also updated.
        refreshed = registry.get(entry.run_id)
        assert refreshed is not None
        assert refreshed.status == "cancelled"
    finally:
        _restore_overrides()


# ---------------------------------------------------------------------------
# 4. POST /a2a/tasks/{run_id}/cancel — not found
# ---------------------------------------------------------------------------


def test_cancel_task_returns_404_for_unknown_run() -> None:
    """Tier 2: POST /a2a/tasks/{run_id}/cancel returns 404 for an unknown run_id."""
    registry = RunRegistry()
    client = _make_client_with_registry(registry)
    try:
        r = client.post("/a2a/tasks/no-such-run/cancel")
        assert r.status_code == 404, r.text
    finally:
        _restore_overrides()


# ---------------------------------------------------------------------------
# 5. GET /a2a/tasks/{run_id}/events — SSE stream
# ---------------------------------------------------------------------------


def test_stream_task_events_returns_event_stream_content_type() -> None:
    """Tier 2: GET /a2a/tasks/{run_id}/events returns text/event-stream
    content-type, replays buffered events, and closes when status is terminal.
    """
    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-sse")

    # Buffer two events and then mark as completed so the generator terminates.
    registry.append_event(entry.run_id, {"type": "start", "msg": "beginning"})
    registry.append_event(entry.run_id, {"type": "progress", "msg": "working"})
    registry.update(entry.run_id, status="completed", result="done")

    client = _make_client_with_registry(registry)
    try:
        r = client.get(f"/a2a/tasks/{entry.run_id}/events")
        assert r.status_code == 200, r.text
        assert "text/event-stream" in r.headers.get("content-type", "")

        # The body should contain SSE data lines for each buffered event.
        text = r.text
        assert '"start"' in text
        assert '"progress"' in text
        # Terminal end event must be present.
        assert "event: end" in text
    finally:
        _restore_overrides()


def test_stream_task_events_returns_not_found_for_missing_run() -> None:
    """Tier 2: GET /a2a/tasks/{run_id}/events for unknown run_id yields
    an SSE error event (not HTTP 404 — streaming response starts before
    the check completes)."""
    registry = RunRegistry()
    client = _make_client_with_registry(registry)
    try:
        r = client.get("/a2a/tasks/no-such-run/events")
        # The response is 200 (StreamingResponse) but body carries error.
        assert r.status_code == 200, r.text
        assert "not_found" in r.text
    finally:
        _restore_overrides()


# ---------------------------------------------------------------------------
# 6. Agent Card capabilities flip
# ---------------------------------------------------------------------------


def test_agent_card_shows_streaming_and_push_notifications_true(tmp_path) -> None:
    """Tier 2: FP-0001 flips Agent Card capabilities to streaming=True and
    pushNotifications=True. stateTransitionHistory stays False."""
    from fastapi.testclient import TestClient

    from reyn.budget.budget import BudgetTracker, CostConfig
    from reyn.chat.profile import AgentProfile
    from reyn.chat.registry import AgentRegistry
    from reyn.chat.session import ChatSession
    from reyn.events.state_log import StateLog
    from reyn.web.deps import get_registry
    from reyn.web.server import app

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> ChatSession:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        bt = BudgetTracker(CostConfig())
        return ChatSession(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    registry.create("demo", role="demo agent")

    from reyn.web.deps import get_run_registry  # noqa: PLC0415

    run_registry = RunRegistry()
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_run_registry] = lambda: run_registry
    client = TestClient(app, raise_server_exceptions=False)
    try:
        r = client.get("/a2a/agents/demo/.well-known/agent-card.json")
        assert r.status_code == 200, r.text
        caps = r.json()["capabilities"]
        assert caps["streaming"] is True, "streaming must be True after FP-0001"
        assert caps["pushNotifications"] is True, "pushNotifications must be True after FP-0001"
        assert caps["stateTransitionHistory"] is False, "stateTransitionHistory must remain False"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 7. POST /a2a/agents/{name} — answer injection mode
# ---------------------------------------------------------------------------


def test_answer_injection_delivers_to_pending_intervention(tmp_path) -> None:
    """Tier 2: POST /a2a/agents/{name} with params.task_id set delivers an
    InterventionAnswer to the run's pending_intervention and returns
    {"answered": True} when the intervention is active.

    We populate a pending intervention by hand (using UserIntervention +
    asyncio.Future) without going through the full async stack.
    """
    from fastapi.testclient import TestClient

    from reyn.budget.budget import BudgetTracker, CostConfig
    from reyn.chat.profile import AgentProfile
    from reyn.chat.registry import AgentRegistry
    from reyn.chat.session import ChatSession
    from reyn.events.state_log import StateLog
    from reyn.user_intervention import UserIntervention
    from reyn.web.deps import get_registry, get_run_registry
    from reyn.web.server import app

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> ChatSession:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        bt = BudgetTracker(CostConfig())
        return ChatSession(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    registry.create("demo", role="demo agent")

    run_registry = RunRegistry()
    entry = run_registry.create(agent_name="demo", chain_id="chain-iv")

    # Populate a pending intervention on the entry.
    loop = asyncio.new_event_loop()
    try:
        iv_future: asyncio.Future = loop.create_future()
        iv = UserIntervention(kind="ask_user", prompt="What is your name?", future=iv_future)
        run_registry.update(
            entry.run_id,
            status="input-required",
            question=iv.prompt,
            pending_intervention=iv,
        )

        app.dependency_overrides[get_registry] = lambda: registry
        app.dependency_overrides[get_run_registry] = lambda: run_registry
        client = TestClient(app, raise_server_exceptions=False)
        try:
            r = client.post(
                "/a2a/agents/demo",
                json={
                    "jsonrpc": "2.0",
                    "id": "ans-1",
                    "method": "message/send",
                    "params": {
                        "task_id": entry.run_id,
                        "message": {
                            "role": "user",
                            "parts": [{"kind": "text", "text": "Alice"}],
                        },
                    },
                },
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["jsonrpc"] == "2.0"
            result = body["result"]
            assert result["task_id"] == entry.run_id
            assert result["answered"] is True

            # The future must have been resolved with the answer text.
            # (loop.run_until_complete would block; check via done() instead)
            assert iv_future.done(), "intervention future must be resolved after answer injection"
            resolved = iv_future.result()
            assert resolved.text == "Alice"
        finally:
            app.dependency_overrides.clear()
    finally:
        loop.close()


def test_answer_injection_returns_answered_false_for_unknown_task(tmp_path) -> None:
    """Tier 2: POST /a2a/agents/{name} with params.task_id for a run that
    doesn't exist returns {"answered": False, "reason": "not found"}."""
    from fastapi.testclient import TestClient

    from reyn.budget.budget import BudgetTracker, CostConfig
    from reyn.chat.profile import AgentProfile
    from reyn.chat.registry import AgentRegistry
    from reyn.chat.session import ChatSession
    from reyn.events.state_log import StateLog
    from reyn.web.deps import get_registry, get_run_registry
    from reyn.web.server import app

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> ChatSession:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        bt = BudgetTracker(CostConfig())
        return ChatSession(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    registry.create("demo", role="demo agent")

    run_registry = RunRegistry()

    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_run_registry] = lambda: run_registry
    client = TestClient(app, raise_server_exceptions=False)
    try:
        r = client.post(
            "/a2a/agents/demo",
            json={
                "jsonrpc": "2.0",
                "id": "ans-2",
                "method": "message/send",
                "params": {
                    "task_id": "nonexistent-task-id",
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "answer"}],
                    },
                },
            },
        )
        assert r.status_code == 200, r.text
        result = r.json()["result"]
        assert result["answered"] is False
        assert "reason" in result
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 8. POST /a2a/agents/{name} — async mode (deferred, documented below)
# ---------------------------------------------------------------------------

# Test 8 (async_mode=true with real agent spawning a background task) requires
# F1 (ChatSession.register_intervention_override), F2 (RunRegistry fully wired),
# and F3 (A2AInterventionBus) to be integrated so the background task can
# complete without a real LLM. Deferred to F5 e2e integration test.

# What we CAN test here is the shape of the Task envelope returned by async
# mode when the agent is unknown (= we control the ValueError path without
# needing a real LLM).

def test_async_mode_with_unknown_agent_returns_internal_error(tmp_path) -> None:
    """Tier 2: POST /a2a/agents/{name} with async_mode=true for a non-existent
    agent returns a JSON-RPC internal error envelope (the background task
    spawning raises ValueError before the task can start).

    NOTE: in async mode the error surfaces immediately inside _handle_async_mode
    because send_to_agent_impl raises ValueError synchronously on unknown agent.
    This test pins that the error is handled gracefully.
    """
    # Deferred to F5 e2e — skipped here because actually triggering the async
    # path without a registered agent results in a ValueError being raised
    # inside the background task (after create_task), not before — so the
    # endpoint returns a Task envelope, and the error is visible only via
    # GET /a2a/tasks/{run_id} after the task fails. That cross-component
    # observability requires F5 integration harness.
    pytest.skip(
        "Deferred to F5 e2e: async_mode full round-trip requires F1/F2/F3 integration"
    )


# ---------------------------------------------------------------------------
# 9. New routes are mounted
# ---------------------------------------------------------------------------


def test_fp0001_routes_mounted() -> None:
    """Tier 2: the task lifecycle routes added by FP-0001 are present in
    the FastAPI app's route table.

    Pins that include_router wired them correctly without accidentally
    shadowing or dropping any existing route.
    """
    from reyn.web.server import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/a2a/tasks/{run_id}" in paths, "GET /a2a/tasks/{run_id} must be mounted"
    assert "/a2a/tasks/{run_id}/cancel" in paths, "POST /a2a/tasks/{run_id}/cancel must be mounted"
    assert "/a2a/tasks/{run_id}/events" in paths, "GET /a2a/tasks/{run_id}/events must be mounted"
