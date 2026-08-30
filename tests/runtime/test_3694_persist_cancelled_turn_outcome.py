"""Tier 2: #3694 (owner-ratified design, architect + 2026-08-08) — a
genuinely-cancelled turn's outcome is persisted as a durable, typed history
entry, never fabricated as assistant content, and never reaches the LLM.

Storage shape mirrors ``Session.notify_state_change`` exactly (same owner
ruling: "むやみに増やすべきでない、system あるならそれで" — no new role):
``role="system"`` + ``meta={"kind": "turn_cancelled", "chain_id": ...}``.

Two independent stamp sites exist because measurement showed they are NOT
redundant — a hard cancel (``cancel_inflight()``'s ``Task.cancel()``, the
common mid-LLM-call Ctrl+C case) injects ``CancelledError`` at whatever
await the turn was suspended on, which unwinds straight past
``RouterLoop.run_loop``'s own cooperative-cancel terminal (zero
``CancelledError`` handling anywhere in ``router_loop.py`` — measured via
grep). ``Session.run_one_iteration``'s own catch is the receiver for that
case; ``RouterLoop.run_loop``'s ``if _loop_cancelled:`` terminal is the
primary path for a cooperative-only cancel (the LLM call itself was never
interrupted — the loop-head check caught the request first).

Witnesses:
(a) shape — ``notify_turn_cancelled`` persists the exact typed record.
(b) LLM non-reach — structurally guaranteed by the existing role
    allowlist (``build_history``), verified directly, not assumed.
(c) compaction non-candidate — same allowlist, ``force_compact_now``'s own
    turns filter.
(d) restore rescue — the entry is NOT silently dropped by
    ``restore.py``'s ``_SKIP_ROLES``, while an ordinary ``state_change``
    system entry (a DIFFERENT ``meta.kind``) still IS — the rescue is
    keyed on ``meta.kind``, not on role.
(e) cooperative-cancel wiring (① — ``RouterLoop.run_loop``'s own terminal)
    driven through the REAL ``RouterLoop`` (no Session), via the same
    ``FakeRouterHost``/``arm_cancel_after`` seam ``test_turn_cancel_1468.py``
    already uses.
(f) non-fabrication — a cancel from something OTHER than
    ``cancel_inflight()`` (``_turn_cancel_self_initiated`` False) must NOT
    be recorded as a user cancel.
(g) hard-cancel wiring (② — ``Session.run_one_iteration``'s
    ``CancelledError`` catch), driven through a REAL ``Session`` via
    ``cancel_inflight()`` against an in-flight (hung) turn — the same
    controllable-hang seam ``tests/core/test_2242_hard_cancel.py`` uses. Kept
    HERE (not only cross-referenced) after review caught that this file's
    own suite went fully green with ② stripped to a no-op — the positive
    witness lived exclusively in test_2242_hard_cancel.py's UPDATED
    assertion, invisible to anyone reading this file in isolation.

``tests/core/test_2242_hard_cancel.py``'s own end-to-end hard-cancel test was
ALSO updated in this same PR (its pre-existing assertion pinned "only the
user message survives a hard cancel", now correctly "user message + the
marker") — that update is corroborating evidence, not this file's sole
witness for ②.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat.restore import project_restored_frames
from reyn.llm.pricing import TokenUsage
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer
from tests._support.agent_session import make_session as _make_hard_cancel_session
from tests._support.events import settle
from tests._support.router_loop import FakeRouterHost, text_result
from tests._support.router_loop import ScriptedLLM as _ScriptedLLM
from tests._support.session import make_session as _make_session


def _collect(session) -> list:
    """Subscribe through the public seam (Session.subscribe_audit_events,
    #5260) — for witness ②: turn_started proves the REAL driver ran (#5450)."""
    collected: list = []
    session.subscribe_audit_events(collected.append)
    return collected


# ── (a) shape ────────────────────────────────────────────────────────────


def test_notify_turn_cancelled_persists_typed_system_entry(tmp_path, monkeypatch):
    """Tier 2: arm (a) — the persisted shape matches the design exactly."""
    session = _make_session(tmp_path, monkeypatch=monkeypatch)
    session.notify_turn_cancelled("chain-abc")

    (entry,) = session.history
    assert entry.role == "system"
    assert entry.meta.get("kind") == "turn_cancelled"
    assert entry.meta.get("chain_id") == "chain-abc"
    assert entry.text  # a non-empty display string for restore/live rendering


# ── (b) LLM non-reach ────────────────────────────────────────────────────


def test_cancelled_marker_never_reaches_build_history(tmp_path, monkeypatch):
    """Tier 2: arm (b) — RouterHistoryBuffer.build_history (the REAL
    LLM-facing wire builder) excludes the marker, structurally, via the
    same role allowlist every other system entry is already excluded by."""
    session = _make_session(tmp_path, monkeypatch=monkeypatch)
    session._append_history(ChatMessage(role="user", content="q1", ts="t1"))
    session.notify_turn_cancelled("chain-xyz")
    session._append_history(ChatMessage(role="user", content="q2", ts="t2"))

    buf = RouterHistoryBuffer(
        history_fn=lambda: session.history,
        compaction=session._compaction,
        compaction_controller=session._compaction_controller,
        model_fn=lambda: session._resolver.resolve(session.model).model,
        # #5467: RouterHistoryBuffer's own ``events`` constructor param
        # requires a real EventLog (production wiring, not a test-side
        # observer) — collect_events()/settle() have no meaning here, there
        # is nothing to subscribe to or drain. Out of #5467's scope.
        events=session._audit_events,
        media_store=session._media_store,
        router_host=session._router_host,
        universal_wrappers_enabled=session._universal_wrappers_enabled,  # #4552 PR-3
        non_interactive=session._non_interactive,
        reasoning=session._reasoning,
        project_dir_fn=lambda: tmp_path,
    )
    wire = buf.build_history()
    assert all(
        "Turn interrupted by user." not in str(m.get("content", "")) for m in wire
    ), f"the cancelled marker must never appear in the LLM-facing wire list; got {wire!r}"
    roles = [m["role"] for m in wire]
    assert "system" not in roles


# ── (c) compaction non-candidate ─────────────────────────────────────────


def test_cancelled_marker_is_not_a_compaction_candidate(tmp_path, monkeypatch):
    """Tier 2: arm (c) — force_compact_now's own turns filter (the same
    role allowlist) excludes the marker from ever being folded into a
    compaction summary."""
    session = _make_session(tmp_path, monkeypatch=monkeypatch, t_max=20_000)
    filler = "middle turn padding text " * 20
    for i in range(30):
        role = "user" if i % 2 == 0 else "assistant"
        session._append_history(ChatMessage(role=role, content=f"{filler} #{i}", ts=f"t{i}"))
    session.notify_turn_cancelled("chain-mid")

    turns = list(session.history)
    controller = session._compaction_controller
    candidates = controller._select_candidates(turns, prev_cover=0)
    assert all(t.role != "system" for t in candidates), (
        "the cancelled marker (role=system) must never appear in the "
        "compaction candidate set"
    )


# ── (d) restore rescue, and falsify: state_change stays skipped ─────────


def test_cancelled_marker_is_rescued_by_restore_but_state_change_is_not(tmp_path, monkeypatch):
    """Tier 2: arm (d) — project_restored_frames rescues the cancelled
    marker specifically by meta.kind, NOT by role — a state_change system
    entry (a DIFFERENT meta.kind, same role) must stay skipped, proving the
    rescue is not a blanket "let system through" change."""
    session = _make_session(tmp_path, monkeypatch=monkeypatch)
    session._append_history(ChatMessage(role="user", content="q1", ts="t1"))
    session.notify_turn_cancelled("chain-restore")
    session.notify_state_change("MCP server 'x' installed.")

    frames = project_restored_frames(session.history)
    kinds_and_texts = [(f.kind, f.text) for f in frames]

    assert any(
        k == "system" and "Turn interrupted by user." in t for k, t in kinds_and_texts
    ), f"the cancelled marker must be projected; got {kinds_and_texts!r}"
    assert not any(
        "installed" in t for _, t in kinds_and_texts
    ), f"a state_change entry must stay skipped (not rescued); got {kinds_and_texts!r}"


# ── (e) cooperative-cancel wiring (①), driven through the REAL RouterLoop ──


def _loop(host, llm, max_iterations=5):
    return RouterLoop(
        host=host, chain_id="chain-cooperative-cancel",
        max_iterations=max_iterations, llm_caller=llm,
    )


class _CancellableHost(FakeRouterHost):
    def __init__(self) -> None:
        super().__init__()
        self._cancel_after_n = None
        self._iteration_count = 0

    def arm_cancel_after(self, n: int) -> None:
        self._cancel_after_n = n

    def _is_turn_cancel_requested(self) -> bool:
        self._iteration_count += 1
        if self._cancel_after_n is None:
            return False
        return self._iteration_count > self._cancel_after_n


@pytest.mark.asyncio
async def test_cooperative_cancel_persists_the_marker_via_real_router_loop() -> None:
    """Tier 2: arm (e) — driving the REAL RouterLoop.run_loop through a
    cooperative cancel (mirrors test_turn_cancel_1468.py's own seam)
    results in the marker landing on host.history via the real
    append_history_entry call this PR added at the ``if _loop_cancelled:``
    terminal.

    Falsification (performed for real): removing the new
    ``self.host.append_history_entry(...)`` call from that terminal makes
    this test go RED (``host.history`` stays empty)."""
    host = _CancellableHost()
    host.arm_cancel_after(0)  # cancel on the FIRST iteration check
    llm = _ScriptedLLM([text_result("should not run")])
    loop = _loop(host, llm)

    usage = await loop.run_loop(
        messages=[{"role": "user", "content": "hi"}], tools=[], _univ_enabled=False,
    )
    assert isinstance(usage, TokenUsage)
    assert llm.call_count == 0  # cancel fired before any LLM call

    cancelled_entries = [
        e for e in host.history
        if e["role"] == "system" and e["meta"].get("kind") == "turn_cancelled"
    ]
    # Exactly one — unpacking to a single-element tuple raises ValueError
    # otherwise (mirrors the stage-2 #3783 precedent for this idiom).
    (only,) = cancelled_entries
    assert only["meta"].get("chain_id") == "chain-cooperative-cancel"


# ── (f) non-fabrication: a non-user-initiated cancel must not stamp ──────


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_a_non_self_initiated_cancel_does_not_fabricate_a_user_cancel_marker(
    tmp_path, monkeypatch, _llm_stub,
):
    """Tier 2: arm (f) — Session.run_one_iteration's hard-cancel catch only
    calls notify_turn_cancelled when the cancellation was
    self._turn_cancel_self_initiated (i.e. genuinely cancel_inflight()).
    A cancel from something else (a "not self-initiated" cancel — #3377's
    survivable-but-unexpected case, e.g. an unrelated caller's
    ``Task.cancel()``) must not fabricate a "user cancelled this" record.

    Delivered through the REAL mechanism ``test_3377_run_loop_survives_turn_
    cancel.py`` itself uses: ``session._turn_owner_task.cancel()`` directly
    — bypassing ``cancel_inflight()`` entirely, so
    ``_turn_cancel_self_initiated`` stays False. Real ``Session``/real
    ``RouterLoopDriver``/``RouterLoop`` (#5450: the LLM boundary is hung via
    ``@pytest.mark.llm_stub(control="gated")``, not a private ``run_turn``
    replacement).

    Falsification (performed for real): removing the ``else:`` gate
    (calling ``notify_turn_cancelled`` unconditionally) makes this test go
    RED — a marker appears even though nothing that looked like a user
    cancel ever happened.
    """
    session = _make_session(tmp_path, monkeypatch=monkeypatch)
    events = _collect(session)

    await session._put_inbox("user", {"text": "hello", "chain_id": "c-unknown-origin"})
    turn_task = asyncio.create_task(session.run_one_iteration())
    try:
        await _llm_stub.call_started.wait()

        # The REAL "not self-initiated" delivery mechanism — NOT cancel_inflight().
        session._turn_owner_task.cancel()

        _llm_stub.release.set()
        completed = await turn_task
        assert completed is True  # the driver survives (per #3377)
    finally:
        _llm_stub.release.set()

    # witness ②: the real driver dispatched before the cancel hit it.
    await settle(session)
    assert any(e.type == "turn_started" for e in events)

    cancelled_markers = [
        m for m in session.history
        if m.role == "system" and m.meta.get("kind") == "turn_cancelled"
    ]
    assert cancelled_markers == [], (
        f"a cancel from something other than cancel_inflight() must NOT "
        f"fabricate a user-cancel marker; got {cancelled_markers!r}"
    )


# ── (g) hard-cancel wiring (②), driven through a REAL Session ────────────


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_hard_cancel_via_cancel_inflight_persists_the_marker(
    tmp_path, _llm_stub,
) -> None:
    """Tier 2: arm (g) — the ② receiver, witnessed IN THIS FILE (not only
    cross-referenced) so a reader/reviewer of this file alone sees the
    positive path for the "more common" case the module docstring itself
    names.

    Falsification (performed for real, per lead-coder/architect co-vet):
    replacing ``session.notify_turn_cancelled(...)`` at the
    ``_turn_cancel_self_initiated`` branch in ``Session.run_one_iteration``
    with a no-op makes ALL 6 of this file's PRE-EXISTING tests stay green
    (none of arms (a)-(f) exercise ② at all) while
    ``test_2242_hard_cancel.py``'s own updated assertion goes RED — proving
    ② had a real but not-self-contained witness. This test closes that gap:
    the SAME strip makes THIS test go RED on its own, restored clean."""
    session = _make_hard_cancel_session(
        agent_name="hard-cancel-3694-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snapshot.json",
    )
    events = _collect(session)

    await session._put_inbox("user", {"text": "hello", "chain_id": "c-hard-cancel-g"})
    turn_task = asyncio.create_task(session.run_one_iteration())
    try:
        await _llm_stub.call_started.wait()

        # The REAL delivery mechanism: cancel_inflight() (NOT a raw
        # Task.cancel()) — sets _turn_cancel_self_initiated=True FIRST,
        # which is what gates notify_turn_cancelled's call.
        result = await session.cancel_inflight()
        assert "cancel" in result.lower()

        _llm_stub.release.set()
        completed = await turn_task
        assert completed is True
    finally:
        _llm_stub.release.set()

    # witness ②: the real driver dispatched before the cancel hit it.
    await settle(session)
    assert any(e.type == "turn_started" for e in events)

    (marker,) = [
        m for m in session.history
        if m.role == "system" and m.meta.get("kind") == "turn_cancelled"
    ]
    assert marker.meta.get("chain_id") == "c-hard-cancel-g"
