"""Tier 1 contract tests for RunRegistry (FP-0001).

Verifies the public API surface of ``reyn.web.run_registry.RunRegistry`` and
``RunEntry``. No mocks, no MagicMock / AsyncMock / patch — real instances
throughout.

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- Tier 1: pins the public Python API surface of the run_registry module.
- No unittest.mock / MagicMock / AsyncMock / patch usage.
- Real asyncio.Task instances (via asyncio.create_task on no-op coroutines).
- Real UserIntervention / InterventionAnswer from reyn.user_intervention.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.user_intervention import InterventionAnswer, UserIntervention
from reyn.web.run_registry import RunEntry, RunRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry() -> RunRegistry:
    return RunRegistry()


def _make_entry(registry: RunRegistry, *, agent_name: str = "agent-a", chain_id: str = "c1") -> RunEntry:
    return registry.create(agent_name=agent_name, chain_id=chain_id)


async def _noop() -> None:
    """A no-op coroutine for creating real asyncio.Task instances."""


# ---------------------------------------------------------------------------
# Test 1: create() allocates unique run_id and sets status="running"
# ---------------------------------------------------------------------------


def test_create_allocates_run_id_and_sets_running_status() -> None:
    """Tier 1: create() allocates a UUID hex run_id, status='running', chain_id echoed."""
    registry = _make_registry()
    entry = registry.create(agent_name="agent-x", chain_id="chain-42")

    assert isinstance(entry.run_id, str)
    assert len(entry.run_id) == 32  # uuid4().hex is 32 hex chars
    assert all(c in "0123456789abcdef" for c in entry.run_id)
    assert entry.status == "running"
    assert entry.agent_name == "agent-x"
    assert entry.chain_id == "chain-42"


def test_create_allocates_unique_run_ids() -> None:
    """Tier 1: successive create() calls produce distinct run_ids."""
    registry = _make_registry()
    ids = {registry.create(agent_name="a", chain_id="c").run_id for _ in range(10)}
    assert len(ids) == 10


# ---------------------------------------------------------------------------
# Test 2: get() returns entry or None for unknown
# ---------------------------------------------------------------------------


def test_get_returns_entry_for_known_run_id() -> None:
    """Tier 1: get(run_id) returns the RunEntry created earlier."""
    registry = _make_registry()
    entry = _make_entry(registry)
    fetched = registry.get(entry.run_id)
    assert fetched is entry


def test_get_returns_none_for_unknown_run_id() -> None:
    """Tier 1: get() returns None when the run_id is not registered."""
    registry = _make_registry()
    assert registry.get("nonexistent-id") is None


# ---------------------------------------------------------------------------
# Test 3: list() — all entries and filtered by agent_name
# ---------------------------------------------------------------------------


def test_list_returns_all_entries_when_no_filter() -> None:
    """Tier 1: list() with no args returns all registered entries."""
    registry = _make_registry()
    e1 = registry.create(agent_name="alpha", chain_id="c1")
    e2 = registry.create(agent_name="beta", chain_id="c2")
    e3 = registry.create(agent_name="alpha", chain_id="c3")

    all_entries = registry.list()
    assert set(e.run_id for e in all_entries) == {e1.run_id, e2.run_id, e3.run_id}


def test_list_filters_by_agent_name() -> None:
    """Tier 1: list(agent_name='x') returns only entries for that agent."""
    registry = _make_registry()
    e1 = registry.create(agent_name="alpha", chain_id="c1")
    registry.create(agent_name="beta", chain_id="c2")
    e3 = registry.create(agent_name="alpha", chain_id="c3")

    alpha_entries = registry.list(agent_name="alpha")
    assert set(e.run_id for e in alpha_entries) == {e1.run_id, e3.run_id}

    beta_entries = registry.list(agent_name="beta")
    assert len(beta_entries) == 1

    missing_entries = registry.list(agent_name="gamma")
    assert missing_entries == []


# ---------------------------------------------------------------------------
# Test 4: update() mutates fields and bumps updated_at
# ---------------------------------------------------------------------------


def test_update_mutates_status_and_result() -> None:
    """Tier 1: update(status='completed', result='hi') sets both fields."""
    registry = _make_registry()
    entry = _make_entry(registry)
    original_updated_at = entry.updated_at

    returned = registry.update(entry.run_id, status="completed", result="hi")

    assert returned is entry
    assert entry.status == "completed"
    assert entry.result == "hi"
    assert entry.updated_at >= original_updated_at


def test_update_returns_none_for_unknown_run_id() -> None:
    """Tier 1: update() returns None when the run_id does not exist."""
    registry = _make_registry()
    result = registry.update("no-such-id", status="completed")
    assert result is None


# ---------------------------------------------------------------------------
# Test 5: attach_task() stores a real asyncio.Task reference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_task_stores_task_reference() -> None:
    """Tier 1: attach_task(run_id, task) stores real asyncio.Task on RunEntry."""
    registry = _make_registry()
    entry = _make_entry(registry)

    task = asyncio.create_task(_noop())
    registry.attach_task(entry.run_id, task)

    assert entry.task is task
    await task  # clean up


# ---------------------------------------------------------------------------
# Test 6: answer_intervention() resolves pending IV's future
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_intervention_resolves_future_and_clears_question() -> None:
    """Tier 1: answer_intervention delivers answer to IV future; status → 'running'; question cleared."""
    registry = _make_registry()
    entry = _make_entry(registry)

    iv = UserIntervention(kind="ask_user", prompt="What is your name?")
    entry.pending_intervention = iv
    entry.question = "What is your name?"
    entry.status = "input-required"

    answer = InterventionAnswer(text="hi")
    result = registry.answer_intervention(entry.run_id, answer)

    assert result is True
    assert iv.future.done()
    assert iv.future.result().text == "hi"
    assert entry.pending_intervention is None
    assert entry.question is None
    assert entry.status == "running"


# ---------------------------------------------------------------------------
# Test 7: answer_intervention() returns False when no pending IV
# ---------------------------------------------------------------------------


def test_answer_intervention_returns_false_when_no_pending_iv() -> None:
    """Tier 1: answer_intervention returns False when entry has no pending_intervention."""
    registry = _make_registry()
    entry = _make_entry(registry)
    # No pending_intervention set (default None)

    result = registry.answer_intervention(entry.run_id, InterventionAnswer(text="ignored"))
    assert result is False


def test_answer_intervention_returns_false_for_unknown_run_id() -> None:
    """Tier 1: answer_intervention returns False when run_id doesn't exist."""
    registry = _make_registry()
    result = registry.answer_intervention("no-such-id", InterventionAnswer(text="x"))
    assert result is False


# ---------------------------------------------------------------------------
# Test 8: answer_intervention() returns False when future is already done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_intervention_returns_false_on_duplicate_answer() -> None:
    """Tier 1: answer_intervention returns False when IV future is already resolved."""
    registry = _make_registry()
    entry = _make_entry(registry)

    iv = UserIntervention(kind="ask_user", prompt="Are you sure?")
    entry.pending_intervention = iv
    entry.question = "Are you sure?"
    entry.status = "input-required"

    # First answer — resolves the future
    first_result = registry.answer_intervention(entry.run_id, InterventionAnswer(text="yes"))
    assert first_result is True
    assert iv.future.done()

    # Re-attach the IV (as if it somehow re-appeared) but future is done
    entry.pending_intervention = iv

    # Second answer — future already done → should return False
    second_result = registry.answer_intervention(entry.run_id, InterventionAnswer(text="no"))
    assert second_result is False
    # Original answer preserved
    assert iv.future.result().text == "yes"


# ---------------------------------------------------------------------------
# Test 9: cancel() calls task.cancel() and sets status='cancelled'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_cancels_task_and_sets_status() -> None:
    """Tier 1: cancel(run_id) cancels the attached asyncio.Task and sets status='cancelled'."""
    registry = _make_registry()
    entry = _make_entry(registry)

    # Create a long-running task so it doesn't finish before we cancel
    async def _long_running() -> None:
        await asyncio.sleep(999)

    task = asyncio.create_task(_long_running())
    registry.attach_task(entry.run_id, task)

    result = registry.cancel(entry.run_id)

    assert result is True
    assert entry.status == "cancelled"
    assert task.cancelled() or task.cancelling() > 0

    # Clean up — consume the cancellation
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Test 10: cancel() returns False for unknown run_id
# ---------------------------------------------------------------------------


def test_cancel_returns_false_for_unknown_run_id() -> None:
    """Tier 1: cancel() returns False when run_id is not registered."""
    registry = _make_registry()
    result = registry.cancel("unknown-run-id")
    assert result is False


# ---------------------------------------------------------------------------
# Test 11: append_event() appends to history_events
# ---------------------------------------------------------------------------


def test_append_event_appends_to_history_events() -> None:
    """Tier 1: append_event(run_id, ev) appends the dict to entry.history_events."""
    registry = _make_registry()
    entry = _make_entry(registry)

    assert entry.history_events == []

    ev1 = {"type": "phase_started", "phase": "plan"}
    ev2 = {"type": "phase_completed", "phase": "plan"}
    registry.append_event(entry.run_id, ev1)
    registry.append_event(entry.run_id, ev2)

    assert entry.history_events == [ev1, ev2]


def test_append_event_no_op_for_unknown_run_id() -> None:
    """Tier 1: append_event on unknown run_id silently does nothing."""
    registry = _make_registry()
    # Should not raise
    registry.append_event("no-such-id", {"type": "test"})


# ---------------------------------------------------------------------------
# Test 12: remove() drops the entry
# ---------------------------------------------------------------------------


def test_remove_drops_the_entry() -> None:
    """Tier 1: remove(run_id) removes entry from registry; get() returns None afterwards."""
    registry = _make_registry()
    entry = _make_entry(registry)
    run_id = entry.run_id

    assert registry.get(run_id) is not None
    registry.remove(run_id)
    assert registry.get(run_id) is None


def test_remove_no_op_for_unknown_run_id() -> None:
    """Tier 1: remove() on unknown run_id silently does nothing (no KeyError)."""
    registry = _make_registry()
    registry.remove("nonexistent")  # must not raise


# ---------------------------------------------------------------------------
# Test 13: to_public_dict() returns JSON-safe fields, drops task and IV
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_to_public_dict_returns_json_safe_fields() -> None:
    """Tier 1: to_public_dict() exposes run_id/agent_name/chain_id/status/question/result/error/timestamps; omits task and IV."""
    registry = _make_registry()
    entry = registry.create(agent_name="myagent", chain_id="chain-99", webhook_url="http://example.com")

    # Attach a real task and IV to confirm they're excluded
    task = asyncio.create_task(_noop())
    registry.attach_task(entry.run_id, task)
    iv = UserIntervention(kind="ask_user", prompt="Q?")
    entry.pending_intervention = iv

    d = entry.to_public_dict()

    # Required keys present
    assert d["run_id"] == entry.run_id
    assert d["agent_name"] == "myagent"
    assert d["chain_id"] == "chain-99"
    assert d["status"] == "running"
    assert d["question"] is None
    assert d["result"] is None
    assert d["error"] is None
    assert isinstance(d["created_at"], str)
    assert isinstance(d["updated_at"], str)

    # Internal-only fields must not appear
    assert "task" not in d
    assert "pending_intervention" not in d
    assert "webhook_url" not in d
    assert "history_events" not in d

    await task  # clean up


# ---------------------------------------------------------------------------
# Test 14: updated_at advances monotonically across update calls
# ---------------------------------------------------------------------------


def test_updated_at_advances_monotonically() -> None:
    """Tier 1: updated_at is bumped on each update() call and never goes backwards."""
    registry = _make_registry()
    entry = _make_entry(registry)

    t0 = entry.updated_at

    registry.update(entry.run_id, status="input-required", question="Q?")
    t1 = entry.updated_at

    registry.update(entry.run_id, status="running")
    t2 = entry.updated_at

    registry.update(entry.run_id, status="completed", result="done")
    t3 = entry.updated_at

    assert t1 >= t0
    assert t2 >= t1
    assert t3 >= t2
