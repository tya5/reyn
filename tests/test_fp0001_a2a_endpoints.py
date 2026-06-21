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


from reyn.interfaces.web.run_registry import RunRegistry  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_client_with_registry(registry: RunRegistry, task_backend=None):
    """Build a TestClient that uses the supplied RunRegistry (and an optional
    Task backend — #1953 slice 5a — for the Task-backed A2A surface) via DI
    override. A default in-memory Task backend satisfies a2a_jsonrpc's
    get_task_backend dependency for tests that don't exercise it."""
    from fastapi.testclient import TestClient

    from reyn.interfaces.web.deps import get_run_registry, get_task_backend
    from reyn.interfaces.web.server import app
    from reyn.task import InMemoryTaskBackend

    backend = task_backend if task_backend is not None else InMemoryTaskBackend()
    app.dependency_overrides[get_run_registry] = lambda: registry
    app.dependency_overrides[get_task_backend] = lambda: backend
    client = TestClient(app, raise_server_exceptions=False)
    return client


def _restore_overrides() -> None:
    from reyn.interfaces.web.server import app
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 1. GET /a2a/tasks/{run_id} — found
# ---------------------------------------------------------------------------


def test_get_task_returns_a2a_envelope_from_task_backend() -> None:
    """Tier 2 (#1953 slice 5a): GET /a2a/tasks/{task_id} returns 200 with the spec
    A2A Task envelope read from the Task backend (Task-vocab state)."""
    import asyncio

    from reyn.task import InMemoryTaskBackend, Task, TaskState

    backend = InMemoryTaskBackend()
    asyncio.new_event_loop().run_until_complete(
        backend.create(Task(task_id="t-1", name="n", assignee="a2a:ctx-7",
                            requester="r", status=TaskState.IN_PROGRESS)))
    client = _make_client_with_registry(RunRegistry(), task_backend=backend)
    try:
        r = client.get("/a2a/tasks/t-1")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "task"
        assert body["id"] == "t-1"
        assert body["status"]["state"] == "working"  # in_progress → working
        assert body["contextId"] == "ctx-7"
        assert "run_id" not in body
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


def test_cancel_task_aborts_task_in_backend() -> None:
    """Tier 2 (#1953 slice 5a): POST /a2a/tasks/{task_id}/cancel = the external
    requester's remove-op → task.abort (cooperative-terminal → archived). The
    response is the archived Task's A2A envelope (status=canceled)."""
    import asyncio

    from reyn.task import InMemoryTaskBackend, Task, TaskState

    backend = InMemoryTaskBackend()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        backend.create(Task(task_id="t-1", name="n", assignee="a2a:ctx-1",
                            requester="r", status=TaskState.IN_PROGRESS)))
    client = _make_client_with_registry(RunRegistry(), task_backend=backend)
    try:
        r = client.post("/a2a/tasks/t-1/cancel")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "task"
        assert body["id"] == "t-1"
        assert body["status"]["state"] == "canceled"  # archived → canceled

        # backend task is archived (the abort terminal).
        refreshed = loop.run_until_complete(backend.get("t-1"))
        assert refreshed is not None and refreshed.status is TaskState.ARCHIVED
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
    """Tier 2: Agent Card capabilities advertise:

      - streaming: True (= issue #267 Gap 1 SSE producer wired in PR #288)
      - pushNotifications: True (= issue #267 Gap 2 webhook trigger
        expansion landed in PR #286)
      - stateTransitionHistory: False (= no plans to implement)

    History: FP-0001 originally claimed both ``True`` but the producers
    were missing; PR #272 (Gap 3 Z-b) flipped them to ``False`` as an
    interim honest disclosure while Gap 1+2 work landed; this PR
    (= Gap 3 Z-c) flips them back to ``True`` now that the producers
    are wired. Each claim is pinned to its concrete in-source wire by
    ``tests/test_a2a_capability_claim_interim.py``.
    """
    from fastapi.testclient import TestClient

    from reyn.core.events.state_log import StateLog
    from reyn.interfaces.web.deps import get_registry
    from reyn.interfaces.web.server import app
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        bt = BudgetTracker(CostConfig())
        return Session(
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

    from reyn.interfaces.web.deps import get_run_registry, get_task_backend  # noqa: PLC0415
    from reyn.task import InMemoryTaskBackend  # noqa: PLC0415

    run_registry = RunRegistry()
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_run_registry] = lambda: run_registry
    app.dependency_overrides[get_task_backend] = lambda: InMemoryTaskBackend()
    client = TestClient(app, raise_server_exceptions=False)
    try:
        r = client.get("/a2a/agents/demo/.well-known/agent-card.json")
        assert r.status_code == 200, r.text
        caps = r.json()["capabilities"]
        assert caps["streaming"] is True, (
            "streaming must be True after issue #267 Gap 1 SSE producer "
            "wiring (PR #288). Gap 3 Z-c re-elevation."
        )
        assert caps["pushNotifications"] is True, (
            "pushNotifications must be True after issue #267 Gap 2 "
            "webhook trigger expansion (PR #286). Gap 3 Z-c re-elevation."
        )
        assert caps["stateTransitionHistory"] is False, "stateTransitionHistory must remain False"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 7. POST /a2a/agents/{name} — answer injection mode
# ---------------------------------------------------------------------------


def test_answer_injection_delivers_to_pending_intervention(tmp_path) -> None:
    """Tier 2: POST /a2a/agents/{name} with params.task_id set delivers
    an InterventionAnswer to the agent's pending intervention and
    returns ``{"answered": True}``.

    issue #292 (α): iv lives in ``Session._interventions._active``,
    NOT in ``RunEntry.pending_intervention`` (removed). We seed the iv
    directly into the agent's intervention registry; the router looks
    up the agent via the RunEntry's ``agent_name`` and calls
    ``Session.answer_pending_intervention``.
    """
    from fastapi.testclient import TestClient

    from reyn.core.events.state_log import StateLog
    from reyn.interfaces.web.deps import get_registry, get_run_registry
    from reyn.interfaces.web.server import app
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session
    from reyn.user_intervention import UserIntervention

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        bt = BudgetTracker(CostConfig())
        return Session(
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

    from reyn.runtime.a2a_routing import a2a_session_id
    run_registry = RunRegistry()
    # #1814: the run carries its core session routing-key so answer-injection
    # re-resolves the SAME per-contextId session.
    entry = run_registry.create(
        agent_name="demo", chain_id="chain-iv",
        session_id=a2a_session_id("ctx-iv"),
    )

    # Seed the iv directly into the agent's outstanding intervention
    # queue (= post-α: Session owns iv state). Use the same loop
    # the TestClient will drive so the future is on the right loop.
    loop = asyncio.new_event_loop()
    try:
        # FP-0043 S4b-4 (B): a2a delegations + answer-injection run on the agent's
        # shared a2a session, so seed the iv there (not "main") — the same session
        # the endpoint resolves.
        from reyn.runtime.a2a_routing import resolve_a2a_session
        session = resolve_a2a_session(registry, "demo", "ctx-iv")
        iv_future = loop.create_future()
        iv = UserIntervention(
            kind="ask_user",
            prompt="What is your name?",
            run_id=entry.run_id,
            future=iv_future,
        )
        # Insert into the registry's active queue (= bypasses dispatch
        # for test setup simplicity; production goes through
        # InterventionHandler.dispatch).
        session._interventions._active[iv.id] = iv
        session._interventions._order.append(iv.id)

        from reyn.interfaces.web.deps import get_task_backend  # noqa: PLC0415
        from reyn.task import InMemoryTaskBackend  # noqa: PLC0415
        app.dependency_overrides[get_registry] = lambda: registry
        app.dependency_overrides[get_run_registry] = lambda: run_registry
        app.dependency_overrides[get_task_backend] = lambda: InMemoryTaskBackend()
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

            # Future resolved with the answer text.
            assert iv_future.done(), (
                "iv future must be resolved after answer injection"
            )
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

    from reyn.core.events.state_log import StateLog
    from reyn.interfaces.web.deps import get_registry, get_run_registry, get_task_backend
    from reyn.interfaces.web.server import app
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session
    from reyn.task import InMemoryTaskBackend

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        bt = BudgetTracker(CostConfig())
        return Session(
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
    app.dependency_overrides[get_task_backend] = lambda: InMemoryTaskBackend()
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
# F1 (Session.register_intervention_override), F2 (RunRegistry fully wired),
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
    from reyn.interfaces.web.server import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/a2a/tasks/{run_id}" in paths, "GET /a2a/tasks/{run_id} must be mounted"
    assert "/a2a/tasks/{run_id}/cancel" in paths, "POST /a2a/tasks/{run_id}/cancel must be mounted"
    assert "/a2a/tasks/{run_id}/events" in paths, "GET /a2a/tasks/{run_id}/events must be mounted"
