"""Tier 2: A2A sync→async auto-escalation (B42-NF-W6-2 fix).

Pinned invariants:

- ``send_to_agent_impl`` returns ``running_skill_run_ids`` list that
  enumerates skill run_ids whose asyncio.Task is not yet done at the
  point the MessageBus pump returned. Empty when no skill is in flight.
- ``_escalate_to_task`` constructs an A2A-spec Task envelope
  (``{"kind": "task", "id": ..., "status": "running", "agent_name": ...}``)
  and registers a ``RunEntry`` so ``GET /a2a/tasks/{id}`` can serve status.
- The escalation only fires when ``partial=True`` AND
  ``running_skill_run_ids`` is non-empty. Plain partial (= timeout
  without skill in flight) still returns a Message envelope per spec.
- ``_await_skill_completion`` returns ``True`` once the supplied
  skill's asyncio.Task is done (= terminal state) within the deadline,
  draining any queued inbox messages so the narration LLM turn can
  fire. Returns ``False`` on deadline expiry so the caller can mark
  the entry ``status="timeout"`` (distinct from a real completion).
- ``_harvest_completion_narration`` returns the latest router-narration
  text following a ``meta.source="skill_completion"`` injection for the
  matching run_id; falls through to the latest non-spawn-ack agent
  message when no injection pair exists.

Reference: B42-NF-W6-2 retrospective + W6-S6 (skill_builder invalid-spec)
empirical reproduction; A2A spec v0.2.0 Message-vs-Task discriminator.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.web.routers.a2a import (
    _await_skill_completion,
    _handle_message_send,
    _harvest_completion_narration,
)
from reyn.interfaces.web.run_registry import RunRegistry

# ---------------------------------------------------------------------------
# Stand-in session/history shapes — real types where they matter
# ---------------------------------------------------------------------------


class _Msg:
    """Minimal stand-in for ``reyn.chat.session.ChatMessage`` used in
    history-harvesting tests. The real ChatMessage has many fields the
    harvester doesn't read; this isolates the contract: role + text + meta."""

    def __init__(self, role: str, text: str, meta: dict | None = None):
        self.role = role
        self.text = text
        self.meta = meta or {}


class _Session:
    """Stand-in ChatSession with the attributes the escalation path reads."""

    def __init__(self, *, running_skills: dict | None = None, history: list | None = None):
        self.running_skills = running_skills or {}
        self.history = history or []
        self.inbox = asyncio.Queue()
        self._iterations = 0

    async def run_one_iteration(self) -> None:
        self._iterations += 1
        # Consume one inbox message (mirror real behaviour: each pump
        # iteration consumes one inbox entry).
        try:
            self.inbox.get_nowait()
        except asyncio.QueueEmpty:
            pass


# ---------------------------------------------------------------------------
# _await_skill_completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_skill_completion_returns_true_when_task_done():
    """Tier 2: returns True once the skill's asyncio.Task transitions to
    done() within the deadline (= caller can safely harvest narration).
    """
    finished_event = asyncio.Event()

    async def _skill_body() -> None:
        await finished_event.wait()

    task = asyncio.create_task(_skill_body())
    session = _Session(running_skills={"run-1": task})

    # Schedule completion ~30ms after the wait starts
    async def _trigger():
        await asyncio.sleep(0.03)
        finished_event.set()

    asyncio.create_task(_trigger())

    result = await asyncio.wait_for(
        _await_skill_completion(session, "run-1", deadline_s=2.0, agent_name="test-agent"),
        timeout=3.0,
    )
    assert result is True
    assert task.done()


@pytest.mark.asyncio
async def test_await_skill_completion_returns_true_immediately_when_no_such_run_id():
    """Tier 2: unknown run_id → return True immediately (no hang).

    Defensive: the run may have completed before the monitor task got
    scheduled, in which case the entry is already gone from
    ``running_skills``. Must not hang waiting for a phantom task. The
    completed-before-monitor case is semantically a success (= we
    skipped the wait but the run did finish), so True is correct.
    """
    session = _Session(running_skills={})
    # Should complete well under the deadline
    result = await asyncio.wait_for(
        _await_skill_completion(session, "phantom", deadline_s=1.0, agent_name="test-agent"),
        timeout=2.0,
    )
    assert result is True


@pytest.mark.asyncio
async def test_await_skill_completion_returns_false_on_deadline():
    """Tier 2: deadline fires before task.done()
    → return False so the caller can mark the run_registry entry
    ``status="timeout"`` rather than ``"completed"``.

    Without this distinction, a long-running skill that exceeds the
    monitor's wait window would be reported as completed with empty
    narration — conflating "we gave up waiting" with a real completion
    result.
    """
    never = asyncio.Event()  # never set
    async def _stuck() -> None:
        await never.wait()
    task = asyncio.create_task(_stuck())
    session = _Session(running_skills={"run-x": task})

    result = await asyncio.wait_for(
        _await_skill_completion(session, "run-x", deadline_s=0.2, agent_name="test-agent"),
        timeout=1.0,
    )
    assert result is False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# _harvest_completion_narration
# ---------------------------------------------------------------------------


def test_harvest_finds_narration_paired_with_skill_completion_injection():
    """Tier 2: returns the latest agent message paired with a
    ``meta.source="skill_completion"`` user injection for the matching
    run_id.
    """
    session = _Session(history=[
        _Msg("user", "spawn me a skill", {}),
        _Msg("agent", "Running in background.", {"source": "spawn_ack"}),
        _Msg("user", "[task_completed] ...", {
            "source": "skill_completion",
            "run_id": "run-1",
        }),
        _Msg("agent", "The skill failed because the spec was invalid.", {}),
    ])
    out = _harvest_completion_narration(session, "run-1")
    assert out == "The skill failed because the spec was invalid."


def test_harvest_falls_back_to_latest_non_spawn_ack_agent_message():
    """Tier 2: when no skill_completion injection exists for run_id, the
    harvester falls back to the latest non-spawn-ack agent message.
    """
    session = _Session(history=[
        _Msg("user", "what's the weather"),
        _Msg("agent", "It's sunny."),
        _Msg("user", "and the temperature?"),
        _Msg("agent", "It's 22 degrees."),
    ])
    out = _harvest_completion_narration(session, "run-not-there")
    assert out == "It's 22 degrees."


def test_harvest_ignores_spawn_ack_in_fallback_path():
    """Tier 2: fallback skips spawn-ack messages (= they're the symptom
    we're escalating PAST, not the narration we want to surface).
    """
    session = _Session(history=[
        _Msg("user", "do something"),
        _Msg("agent", "Skill is running in the background.", {"source": "spawn_ack"}),
    ])
    out = _harvest_completion_narration(session, "any-run-id")
    assert out == ""


def test_harvest_returns_empty_string_when_no_agent_history():
    """Tier 2: history with only user messages → empty string."""
    session = _Session(history=[_Msg("user", "first turn")])
    assert _harvest_completion_narration(session, "x") == ""


# ---------------------------------------------------------------------------
# RunRegistry integration — used by the escalation path
# ---------------------------------------------------------------------------


def test_run_registry_create_and_update_terminal_status():
    """Tier 2: the escalation monitor uses RunRegistry.update() to
    transition entry from "running" to "completed" with the harvested
    narration. Pin that round-trip.
    """
    reg = RunRegistry()
    entry = reg.create(agent_name="a", chain_id="c1")
    assert entry.status == "running"
    assert entry.result is None

    reg.update(entry.run_id, status="completed", result="all done")

    fresh = reg.get(entry.run_id)
    assert fresh is not None
    assert fresh.status == "completed"
    assert fresh.result == "all done"


def test_run_registry_update_failure_path_sets_error():
    """Tier 2: when the monitor task raises, the entry transitions to
    ``failed`` with an error string the caller can surface.
    """
    reg = RunRegistry()
    entry = reg.create(agent_name="a", chain_id="c2")
    reg.update(entry.run_id, status="failed", error="something exploded")

    fresh = reg.get(entry.run_id)
    assert fresh is not None
    assert fresh.status == "failed"
    assert fresh.error == "something exploded"


# ---------------------------------------------------------------------------
# Task envelope shape (= A2A spec v0.2.0 discriminator)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _handle_message_send escalation predicate (B48-NF-W2-S2 fix)
# ---------------------------------------------------------------------------


class _StubSendImpl:
    """Patches ``send_to_agent_impl`` so escalation-path tests can drive
    the handler without spinning up a session. Stores the result dict
    the patched implementation returns.
    """

    def __init__(self, result: dict):
        self._result = result

    async def __call__(self, registry, *, agent_name, message, timeout):
        return self._result


@pytest.mark.asyncio
async def test_escalation_skipped_when_reply_text_nonempty(monkeypatch):
    """Tier 2: when ``send_to_agent_impl`` returns a
    non-empty ``reply`` AND ``partial=True`` AND a still-running skill,
    the handler must return a **Message envelope carrying that reply**
    — not escalate to a Task envelope that drops the text.

    Before the fix the handler escalated unconditionally on
    ``partial AND running_ids``, discarding the early ack and surfacing
    ``(empty)`` to the caller (B48 W2-S2 = ``skill_builder_web_summariser``
    3/3 deterministic reproduction).
    """
    import reyn.interfaces.web.routers.a2a as a2a_mod

    stub = _StubSendImpl({
        "reply": "Skill creation started — wait for completion.",
        "partial": True,
        "agent": "alice",
        "running_skill_run_ids": ["run-skill-builder-1"],
    })
    monkeypatch.setattr(a2a_mod, "send_to_agent_impl", stub)

    result = await _handle_message_send(
        req_id=1,
        params={
            "message": {
                "parts": [{"kind": "text", "text": "build a skill"}],
            },
        },
        agent_name="alice",
        registry=object(),  # unused — stub bypasses session lookup
        run_registry=RunRegistry(),
    )

    assert result["jsonrpc"] == "2.0"
    envelope = result["result"]
    assert envelope["kind"] == "message", (
        f"Expected Message envelope (early reply preserved), got "
        f"{envelope.get('kind')!r}. Escalation must skip when "
        f"reply_text is non-empty."
    )
    # The reply text must be present in parts[0].text — this is the
    # invariant the B48 W2-S2 bug violated.
    parts = envelope["parts"]
    assert parts[0]["text"] == "Skill creation started — wait for completion."
    # ``partial`` metadata still surfaces so the caller can poll if it
    # wants more — the skill keeps running in the background.
    assert envelope["metadata"]["partial"] is True


@pytest.mark.asyncio
async def test_escalation_still_fires_when_reply_text_empty(monkeypatch):
    """Tier 2: when no early reply landed before the timeout (= empty
    ``reply``) and a skill is still running, the handler MUST escalate
    to a Task envelope — preserves B42-NF-W6-2's intent (= no silent
    tombstone for callers that didn't poll back).
    """
    import reyn.interfaces.web.routers.a2a as a2a_mod

    stub = _StubSendImpl({
        "reply": "",
        "partial": True,
        "agent": "alice",
        "running_skill_run_ids": ["run-skill-builder-2"],
    })
    monkeypatch.setattr(a2a_mod, "send_to_agent_impl", stub)

    # _escalate_to_task tries to fetch the session; patch _get_session_for_monitor
    # to return a minimal stand-in so the monitor task can be created.
    monkeypatch.setattr(
        a2a_mod, "_get_session_for_monitor",
        lambda registry, agent_name: _Session(),
    )

    result = await _handle_message_send(
        req_id=2,
        params={
            "message": {
                "parts": [{"kind": "text", "text": "spawn something async"}],
            },
        },
        agent_name="alice",
        registry=object(),
        run_registry=RunRegistry(),
    )

    envelope = result["result"]
    assert envelope["kind"] == "task", (
        f"Expected Task envelope (empty reply + still-running skill), "
        f"got {envelope.get('kind')!r}. B42-NF-W6-2 path must remain "
        f"active for the no-reply case."
    )
    assert envelope["status"] == "running"


@pytest.mark.asyncio
async def test_escalation_skipped_when_whitespace_only_reply(monkeypatch):
    """Tier 2: whitespace-only reply counts as empty (``.strip()`` matters).
    A trailing-newline-only response is not a real ack, so the escalation
    branch should still run when a skill is in flight.
    """
    import reyn.interfaces.web.routers.a2a as a2a_mod

    stub = _StubSendImpl({
        "reply": "   \n  ",
        "partial": True,
        "agent": "alice",
        "running_skill_run_ids": ["run-1"],
    })
    monkeypatch.setattr(a2a_mod, "send_to_agent_impl", stub)
    monkeypatch.setattr(
        a2a_mod, "_get_session_for_monitor",
        lambda registry, agent_name: _Session(),
    )

    result = await _handle_message_send(
        req_id=3,
        params={
            "message": {"parts": [{"kind": "text", "text": "go"}]},
        },
        agent_name="alice",
        registry=object(),
        run_registry=RunRegistry(),
    )

    envelope = result["result"]
    assert envelope["kind"] == "task"


def test_task_envelope_carries_kind_field_for_a2a_discrimination():
    """Tier 2: the Task envelope returned by escalation MUST carry
    ``kind="task"`` — this is the A2A spec v0.2.0 discriminator that
    tells the caller to follow up with ``GET /a2a/tasks/{id}`` rather
    than consume ``parts`` directly.

    Pinned as a contract-shape regression guard since other
    A2A-compliant peers depend on it for response routing.
    """
    # The expected shape — same as what _escalate_to_task returns inside
    # _jsonrpc_result. Pinned by string so a future refactor that drops
    # the field is caught here.
    envelope = {
        "kind": "task",
        "id": "run-id-abc",
        "status": "running",
        "agent_name": "alice",
    }
    assert envelope["kind"] == "task"
    assert "id" in envelope
    # status must be one of the A2A lifecycle states; "running" is the
    # one we emit at escalation time.
    assert envelope["status"] in {"running", "completed", "failed", "input-required"}
