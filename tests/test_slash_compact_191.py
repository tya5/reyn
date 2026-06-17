"""Tier 2: OS invariant — /compact slash fires on-demand compaction + reports freed.

#191 enabler: the user-control avoidance mechanism for the conversation-window
dead-end (the LLM `compact` op + the retry_loop backstop being the other routes).
`/compact` runs the session-level compaction directly (user input, not an LLM op)
via the same `force_compact_now` wrapper the compact op uses (`_compact_now_for_op`),
and reports freed tokens + the free window afterwards.

Real instances, no mocks: a real ChatSession with a real CompactionController/
engine; only `litellm.acompletion` (the compaction LLM call) is monkeypatched to a
plain async callable returning a scripted summary. Verifies the slash actually
fires compaction and reports the freed tokens — not LLM behavior.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import litellm

from reyn.budget.budget import BudgetTracker, CostConfig
from reyn.chat.session import ChatMessage, ChatSession
from reyn.config import CompactionConfig
from reyn.core.events.state_log import StateLog
from reyn.slash import REGISTRY

# Compaction summary the engine's litellm call returns; new_turn_seqs lists the
# candidate turn seqs (head=2/tail=2 over 8 turns → candidates 3..6).
_SUMMARY_JSON = json.dumps({
    "topic_arc": "compacted summary of older turns",
    "decisions": [], "pending": [],
    "session_user_facts": [], "artifacts_referenced": [],
    "new_turn_seqs": [3, 4, 5, 6],
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_session(tmp_path) -> ChatSession:
    """Create a ChatSession with a small synthetic T_max to force non-empty candidates.

    #1128 step 3: _select_candidates uses token-budget boundaries from the engine.
    With ``t_max=2000`` and ``section_caps_spec_tokens=0`` (T_SP≈1125, T_comp_SP≈481):
    head_budget≈87, tail_budget≈131, effective_trigger≈570.
    Turns of 'x'*4000 (=1000 tokens) each individually exceed both budgets, so
    the Axis-7 single-oversized-turn rule includes exactly one turn in head and
    one in tail.  With 8 turns: middle=[t1..t6]=6 candidates ≥ min_compact_batch=1.
    """
    import reyn.llm.model_budget as _mb

    original = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: 2000  # type: ignore[assignment]
    try:
        session = ChatSession(
            agent_name="default",
            budget_tracker=BudgetTracker(CostConfig()),
            state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
            compaction_config=CompactionConfig(
                use_chars4_estimate=True,      # deterministic offline token counts
                section_caps_spec_tokens=0,    # keeps B_M positive for small T_max
            ),
            snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
        )
    finally:
        _mb.get_max_input_tokens = original
    return session


def _drain(session) -> list:
    out = []
    while not session.outbox.empty():
        out.append(session.outbox.get_nowait())
    return out


def _reply_text(session) -> str:
    return " ".join(getattr(m, "text", "") for m in _drain(session))


def test_compact_slash_registered() -> None:
    """Tier 2: /compact resolves in the slash registry to its handler."""
    cmd = REGISTRY.get("compact")
    assert cmd is not None
    assert cmd.name == "compact"


def _populate(session) -> None:
    # 8 user turns (seq 1..8 auto-assigned); head=2/tail=2 → candidates 3..6.
    for _ in range(8):
        session._append_history(ChatMessage(role="user", content="x" * 4000, ts=_now()))


def _script_compaction_llm(monkeypatch) -> None:
    async def _fake_acompletion(model, messages, **kw):  # noqa: ANN001, ANN003
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_SUMMARY_JSON))]
        )
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)


def test_compact_now_for_op_real_chat_measurement(tmp_path, monkeypatch) -> None:
    """Tier 2: with a REAL engine (not a stub), _compact_now_for_op on the chat
    axis reports the middle-COMPRESSION metric — summarized_turns>0 and
    compressed_tokens shrunk into a smaller bridge — while router-view
    freed_tokens is ~0 (structural: the router prompt is head+tail turn-bounded,
    so compaction bridges the already-elided middle rather than shrinking the
    view). Closes the #1177/#1182 test gap (those scripted a compact_now stub)."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    _populate(session)
    _script_compaction_llm(monkeypatch)

    result = asyncio.run(session._compact_now_for_op())

    assert result["summarized_turns"] > 0, "compaction must have summarised older turns"
    assert result["compressed_tokens"] > 0
    assert result["compressed_tokens"] > result["bridge_tokens"], (
        "the summary bridge must be smaller than the raw middle it compresses"
    )
    # router-view freed is ~0 for chat (the documented structural finding) — it is
    # NOT the chat signal; assert it doesn't masquerade as a large freeing.
    assert result["freed_tokens"] < result["compressed_tokens"]
    assert any(m.role == "summary" for m in session.history)


def test_compact_slash_reports_compression(tmp_path, monkeypatch) -> None:
    """Tier 2: /compact runs real compaction and reports the summarised-turns +
    raw→bridge compression (not a misleading router-view 'freed' number)."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    _populate(session)
    _script_compaction_llm(monkeypatch)

    asyncio.run(REGISTRY.get("compact").handler(session, ""))

    text = _reply_text(session).lower()
    assert "summaris" in text and "bridge" in text, (
        f"expected a summarised-turns + bridge compression report; got: {text!r}"
    )
    assert any(m.role == "summary" for m in session.history), (
        "compaction must have produced a summary turn in history"
    )


def test_compact_slash_nothing_to_compact(tmp_path, monkeypatch) -> None:
    """Tier 2: with no compactable turns, /compact reports nothing to compact
    (freed=0 path) — never a misleading 'freed' claim."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    # 3 turns < head(2)+tail(2) → no candidates → force_compact_now no-ops.
    for _ in range(3):
        session._append_history(ChatMessage(role="user", content="hi", ts=_now()))

    async def _fail_acompletion(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("no LLM call expected when there is nothing to compact")
    monkeypatch.setattr(litellm, "acompletion", _fail_acompletion)

    asyncio.run(REGISTRY.get("compact").handler(session, ""))

    text = _reply_text(session).lower()
    assert "nothing to compact" in text, f"expected the no-op report; got: {text!r}"
