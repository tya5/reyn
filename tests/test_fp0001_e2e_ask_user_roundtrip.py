"""Tier 2c: FP-0001 A2A ask_user round-trip — contract and component integration.

Pins the FP-0001 task lifecycle contracts:

  1. ``RunRegistry`` + ``RunEntry`` public API: create / get / update /
     answer_intervention / cancel.
  2. ``A2AInterventionBus.request`` publishes to the registry and awaits
     the Future that ``answer_intervention`` resolves — verifying the
     intervention routing contract without a live HTTP server.
  3. ``Session.register_intervention_override`` / ``unregister_intervention_override``
     — the chain-id-scoped override hook that wires an external bus into
     a skill's ask_user path.

Wire protocol smoke test (router-level) is skipped when the async-mode
extensions to ``POST /a2a/agents/{name}`` and the task endpoints
(``GET /a2a/tasks/{run_id}``, ``POST /a2a/tasks/{run_id}/cancel``) are not
yet present in the router — indicated by the absence of
``GET /a2a/tasks/{run_id}`` in the FastAPI route table.  The skip message
flags this for the F4 implementing agent to remove once the endpoints land.

Approach: we exercise the JSON envelopes by driving RunRegistry and
A2AInterventionBus directly, plus the Session override hook with a
scripted fake LLM callable (= real Session, no MagicMock).  This
validates the component-level contracts that the router will rely on without
requiring the async-mode router extensions to be present.

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- Tier 2c (multi-component integration, OS invariant).
- No MagicMock / AsyncMock / patch of real collaborators.
- Real RunRegistry, real A2AInterventionBus, real Session instances.
- Observed via public API: RunEntry.status, RunEntry.question,
  RunEntry.result, RunEntry.to_public_dict(), answer_intervention return value.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup: ensure tests can import from src/
# ---------------------------------------------------------------------------

_WORKTREE_SRC = Path(__file__).parent.parent / "src"
if str(_WORKTREE_SRC) not in sys.path:
    sys.path.insert(0, str(_WORKTREE_SRC))

# Skip the whole module if optional web deps are missing.
pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_run_registry():
    from reyn.interfaces.web.run_registry import RunRegistry
    return RunRegistry()


def _new_a2a_bus(run_id: str, registry):
    from reyn.interfaces.web.a2a_intervention import A2AInterventionBus
    return A2AInterventionBus(run_id=run_id, registry=registry)


def _new_intervention(prompt: str, run_id: str | None = None):
    from reyn.user_intervention import UserIntervention
    return UserIntervention(kind="ask_user", prompt=prompt, run_id=run_id)


def _new_answer(text: str):
    from reyn.user_intervention import InterventionAnswer
    return InterventionAnswer(text=text)


# ---------------------------------------------------------------------------
# Part 1: RunRegistry public API
# ---------------------------------------------------------------------------


def test_run_registry_create_and_get():
    """Tier 2c: RunRegistry.create allocates a run_id and RunEntry.get returns it."""
    registry = _new_run_registry()
    entry = registry.create(agent_name="myagent", chain_id="chain-1")

    assert entry.run_id
    assert entry.agent_name == "myagent"
    assert entry.status == "running"
    assert entry.result is None
    assert entry.error is None
    # issue #292 (α): ``question`` field removed from RunEntry.
    assert not hasattr(entry, "question")

    fetched = registry.get(entry.run_id)
    assert fetched is entry


def test_run_registry_update_status():
    """Tier 2c: RunRegistry.update mutates status. issue #292 (α):
    the ``question`` kwarg is removed — status mirror is the only
    A2A-wrapper-owned field that mutates here.
    """
    registry = _new_run_registry()
    entry = registry.create(agent_name="a", chain_id="c")

    registry.update(entry.run_id, status="input-required")
    assert registry.get(entry.run_id).status == "input-required"

    # Transitioning back to running.
    registry.update(entry.run_id, status="running")
    assert registry.get(entry.run_id).status == "running"


def test_run_registry_to_public_dict_excludes_internals():
    """Tier 2c: RunEntry.to_public_dict excludes asyncio.Task. issue
    #292 (α): ``pending_intervention`` / ``question`` were also
    excluded pre-α; α removes them entirely from RunEntry's data
    shape (= they live in Session).
    """
    registry = _new_run_registry()
    entry = registry.create(agent_name="b", chain_id="c2")
    registry.update(entry.run_id, status="input-required")

    pub = entry.to_public_dict()
    assert "pending_intervention" not in pub
    assert "task" not in pub
    assert "question" not in pub
    assert pub["status"] == "input-required"
    assert "run_id" in pub
    assert "agent_name" in pub


# Tests for ``RunRegistry.answer_intervention`` removed: post-issue-#292,
# the method is gone and peer-answer flows go through
# ``Session.answer_pending_intervention``. See
# tests/test_fp0001_a2a_intervention_bus.py for the post-α coverage.


def test_run_registry_cancel_marks_cancelled():
    """Tier 2c: RunRegistry.cancel marks a run as cancelled; idempotent for unknown."""
    registry = _new_run_registry()
    entry = registry.create(agent_name="e", chain_id="c5")

    result = registry.cancel(entry.run_id)
    assert result is True
    assert registry.get(entry.run_id).status == "cancelled"

    # Idempotent for already-terminal runs.
    result2 = registry.cancel(entry.run_id)
    assert result2 is True

    # Returns False for unknown run_id.
    assert registry.cancel("unknown-run-id") is False


# ---------------------------------------------------------------------------
# Part 2: A2AInterventionBus — routing contract
# ---------------------------------------------------------------------------


# Tests for A2AInterventionBus.request awaiting iv.future + question
# field mirroring removed: post-issue-#292 the bus is a side-effect
# observer (= ``on_dispatch``) that does not own iv resolution. Status
# mirror behaviour is covered by
# tests/test_fp0001_a2a_intervention_bus.py::test_on_dispatch_mirrors_input_required_status.


# ---------------------------------------------------------------------------
# Part 3: Session.register_intervention_override
# ---------------------------------------------------------------------------


def test_chat_session_intervention_override_is_used():
    """Tier 2c: Session.register_intervention_override / unregister pins
    the chain-id-scoped override hook introduced by FP-0001.

    Verifies that when an override bus is registered for a chain_id,
    the session routes the intervention to that bus rather than the
    default ChatInterventionBus.  Uses a minimal scripted stub bus (real
    callable, no MagicMock) to observe the routing.
    """
    import tempfile

    from reyn.core.events.state_log import StateLog
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig
    from reyn.runtime.session import Session, _new_chain_id
    from reyn.user_intervention import InterventionAnswer, InterventionBus, UserIntervention
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_log = StateLog(tmp / "wal.jsonl")
        snapshot_path = tmp / "snap.json"
        bt = BudgetTracker(CostConfig())
        session = Session(
            agent_name="test-agent",
            agent_role="tester",
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            snapshot_path=snapshot_path,
        )

        received_ivs: list[UserIntervention] = []

        class _ScriptedBus:
            """Real stub bus; no MagicMock.  Records interventions received."""

            async def request(self, iv: UserIntervention) -> InterventionAnswer:
                received_ivs.append(iv)
                return InterventionAnswer(text="scripted-answer")

        chain_id = _new_chain_id()
        stub_bus = _ScriptedBus()
        session.register_intervention_override(chain_id, stub_bus)

        # Probe the override via the session's public accessor.
        iv = UserIntervention(kind="ask_user", prompt="hello?", run_id="r1")

        async def _exercise():
            # Simulate what the OS does: look up the override for this chain.
            override = session.get_intervention_override(chain_id)
            assert override is stub_bus, (
                "register_intervention_override must store the bus at the chain_id key"
            )
            return await override.request(iv)

        loop = asyncio.new_event_loop()
        try:
            answer = loop.run_until_complete(_exercise())
        finally:
            loop.close()

        assert answer.text == "scripted-answer"
        assert received_ivs == [iv]

        # After unregister, the chain_id should no longer be present.
        session.unregister_intervention_override(chain_id)
        assert not session.has_intervention_override(chain_id)


# ---------------------------------------------------------------------------
# Part 4: Router-level smoke test (skipped until F4 endpoints land)
# ---------------------------------------------------------------------------


def _router_has_task_endpoints() -> bool:
    """Return True iff the A2A router has GET /a2a/tasks/{run_id} mounted."""
    try:
        from reyn.interfaces.web.server import app
        paths = {getattr(r, "path", None) for r in app.routes}
        return "/a2a/tasks/{run_id}" in paths
    except Exception:
        return False


_SKIP_ROUTER = not _router_has_task_endpoints()
_SKIP_REASON = (
    "F4 router endpoints (GET /a2a/tasks/{run_id}, POST /a2a/tasks/{run_id}/cancel, "
    "async_mode on message/send) are not yet mounted.  Remove this skip once F4 lands."
)


def _build_registry_for_test(tmp_path: Path):
    """Construct a minimal AgentRegistry for router tests.

    Mirrors the pattern from tests/web/test_a2a.py: 'default' is created
    via AgentProfile.new + .save (not registry.create) because AgentRegistry
    may auto-create 'default' on init in some configurations.
    """
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.session import Session

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return Session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    agent_dir = registry._dir / "default"
    AgentProfile.new("default", role="tester").save(agent_dir)
    return registry


@pytest.mark.skipif(_SKIP_ROUTER, reason=_SKIP_REASON)
def test_async_mode_message_send_returns_task_envelope(tmp_path, monkeypatch):
    """Tier 2c: POST /a2a/agents/{name} with async_mode=true returns a task
    envelope {kind: 'task', id: run_id, status: 'running'}.

    Requires F4 router extensions.  Skipped otherwise.
    """
    monkeypatch.chdir(tmp_path)

    from fastapi.testclient import TestClient

    from reyn.interfaces.web.deps import get_registry, get_run_registry
    from reyn.interfaces.web.run_registry import RunRegistry
    from reyn.interfaces.web.server import app

    registry = _build_registry_for_test(tmp_path)
    # FP-0009 B: RunRegistry now lives in the FastAPI lifespan startup.
    # TestClient constructed without ``with ...`` doesn't fire the lifespan,
    # so override the dependency with a real instance for this test.
    run_registry = RunRegistry()
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_run_registry] = lambda: run_registry
    client = TestClient(app, raise_server_exceptions=False)

    try:
        r = client.post(
            "/a2a/agents/default",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "parts": [{"kind": "text", "text": "start task"}],
                    },
                    "async_mode": True,
                },
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("jsonrpc") == "2.0"
        result = body.get("result", {})
        assert result.get("kind") == "task", f"Expected kind=task, got: {result}"
        assert "id" in result, "Task envelope must include a run_id"
        assert result.get("status") == "running"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.skipif(_SKIP_ROUTER, reason=_SKIP_REASON)
def test_get_task_returns_run_entry(tmp_path, monkeypatch):
    """Tier 2c: GET /a2a/tasks/{run_id} returns the RunEntry public dict.

    Pre-populates RunRegistry with a known entry so the test exercises the
    polling endpoint contract without needing a live async task running.
    Requires F4 router extensions.  Skipped otherwise.
    """
    monkeypatch.chdir(tmp_path)

    from fastapi.testclient import TestClient

    from reyn.interfaces.web.deps import get_registry, get_run_registry
    from reyn.interfaces.web.run_registry import RunRegistry
    from reyn.interfaces.web.server import app

    registry = _build_registry_for_test(tmp_path)

    # Pre-populate RunRegistry with a known entry so we can exercise
    # GET /a2a/tasks/{run_id} without needing a live async task.
    # issue #292 (α): ``question`` is removed from the public dict —
    # iv prompt text is exposed via the SSE stream / webhook payload
    # (= history_events buffer), not via the GET-task endpoint.
    run_registry = RunRegistry()
    entry = run_registry.create(agent_name="default", chain_id="test-chain")
    run_registry.update(entry.run_id, status="input-required")

    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_run_registry] = lambda: run_registry
    client = TestClient(app, raise_server_exceptions=False)

    try:
        r = client.get(f"/a2a/tasks/{entry.run_id}")
        assert r.status_code == 200, r.text
        body = r.json()
        # #1811: GetTask returns a spec A2A Task envelope (id + nested status).
        assert body["id"] == entry.run_id
        assert body["status"]["state"] == "input-required"
        assert "question" not in body  # α: field removed (the core contract here)
    finally:
        app.dependency_overrides.clear()
