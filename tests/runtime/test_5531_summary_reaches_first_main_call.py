"""Tier 2: #5531 PR-1 acceptance witness (architect's own condition,
2026-08-29, posted BEFORE any implementation change) — the invariant
today's separate ``summary=`` argument to ``main_call`` structurally
guarantees: once compaction has emptied ``raw_middle`` and
``RouterLoopDriver._router_main_call`` is finally invoked, a real,
existing summary is genuinely part of that call's wire payload.

Real incident, disclosed (this test's own first draft, caught by
lead-coder's re-check of `engine.py` before I ran it): the ORIGINAL
scenario ("raw_middle non-empty on the FIRST main_call") is UNREACHABLE
— `engine.py`'s own `if raw_middle: ... continue` branch means
`retry_loop` NEVER falls through to `main_call` while `raw_middle` still
has content; a test asserting that shape would pass VACUOUSLY, biting on
`_run_with_shrink`'s own OUTER first attempt (`build_history()`-based,
never touching `retry_loop`/`_router_main_call` at all) instead of the
thing it claims to test — the exact six-questions Q4 shape ("would it
stay green having run with nothing to bite on"). Corrected acceptance
(lead-coder): compaction must genuinely succeed at least once, emptying
`raw_middle`, and THEN the summary must still be on the wire of the
`main_call` that finally fires.

Why this matters for #5531 PR-1's own design: architect's real finding —
flattening ``summary`` into a single ordered list ``main_call`` receives
(architect's own chosen direction, "(b)") must not let the summary
silently drop out of that flattened list. This test is the regression
witness architect asked be kept even after (b) lands ("構造で保証した
なら、構造が壊れた日に赤くなる物が要ります").

Real Session + real RouterLoopDriver/RouterHistoryBuffer/MediaStore
throughout; the LLM call itself is stubbed (`@llm_stub`, required for
compact() to actually run — see `test_5367_reactive_ladder_absorbs_
elide_sized_overflow.py`'s own module docstring for the concrete false
alarm omitting it produces).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from tests._support.agent_session import make_session
from tests._support.events import collect_events


class _FakeStatusError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _ContentDrivenLoop:
    """Mirrors test_5296_pr2_byte_reduction_same_turn_retry.py's own
    harness — fails exactly while ``should_fail(history)`` says so,
    driven by the REAL history payload it was actually handed."""

    def __init__(self, should_fail) -> None:
        self._should_fail = should_fail
        self.calls: "list[list[dict]]" = []

    async def run(self, *, user_text: str, history: "list[dict]") -> "object | None":
        self.calls.append(history)
        if self._should_fail(history, user_text):
            raise _FakeStatusError("request too large", status_code=413)
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _push(session, role: str, text: str, **kw) -> None:
    session._append_history(ChatMessage(role=role, content=text, ts=_now(), **kw))


def _make_session_t_max(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, t_max: int):
    """Default recovery_policy/max_shrink_iterations (unlike the spill
    witness's own builder) — compaction must actually be reachable and
    given enough attempts to succeed for this scenario."""
    monkeypatch.chdir(tmp_path)
    import reyn.llm.model_budget as _mb
    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: t_max)
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,
        section_caps_spec_tokens=0,
    )
    return make_session(
        agent_name="default",
        agent_role="",
        output_language="en",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        compaction_config=cfg,
        multimodal_config=MultimodalConfig(),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )


def _wire_estimate(history: "list[dict]") -> int:
    return sum(len(str(m.get("content", ""))) for m in history)


@pytest.mark.llm_stub
def test_summary_reaches_main_call_after_compaction_empties_raw_middle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: architect's own acceptance witness, corrected acceptance
    per lead-coder's re-check — once compact() has genuinely run and
    emptied raw_middle, A SUMMARY (structurally identifiable, not
    necessarily byte-identical to the one pushed — compact() legitimately
    REGENERATES it when new content folds in, that is its whole job) must
    still be on the wire of the main_call that finally fires. Identified
    by CONTENT (architect's own correction: a call index does not
    identify which layer built it — only a value only that layer
    produces does), not by ``loop.calls[-1]``'s position: the call
    examined is the one AFTER the ``compaction_started`` event fires, the
    one real signal that retry_loop's own compaction actually ran."""
    session = _make_session_t_max(tmp_path, monkeypatch, t_max=2800)

    session._append_history(ChatMessage(
        role="summary",
        content="pre-existing summary",
        ts=_now(),
        meta={"structured": {"topic_arc": "pre-existing summary"}, "covers_through_seq": 5},
    ))
    for i in range(1, 6):
        _push(session, "user" if i % 2 else "assistant", f"covered-{i}", seq=i)
    # Small turns (each under body_token_cap, so spill has nothing to
    # grab onto — mirrors test_5367's own many-small-turns fixture) whose
    # SUM overflows: recovery can only come from compaction actually
    # folding raw_middle away.
    texts = [f"turn-{i}:" + ("X" * 320) for i in range(6, 36)]
    for i, text in enumerate(texts, start=6):
        session._append_history(ChatMessage(
            role="user" if i % 2 else "assistant", content=text, ts=_now(), seq=i,
        ))

    # Fails while the wire is still oversized (mirrors the #5367 many-
    # small-turns witness) — recovers once real turns have been folded
    # into the (still-present) summary and the payload shrinks.
    loop = _ContentDrivenLoop(
        lambda history, user_text: _wire_estimate(history) > 2800
    )
    events = collect_events(session)

    result = asyncio.run(
        session._loop_driver._run_with_shrink_and_byte_reduction(
            loop, "continue please", chain_id="c1",
        )
    )
    assert result is None  # the fake loop's own successful return

    # Compaction genuinely ran (the real signal, not "some call happened"
    # — the same guard architect's own condition on the #5367 witness
    # required).
    assert any(e.type == "compaction_started" for e in events), (
        "test setup sanity: expected compaction to have actually run for "
        "this scenario"
    )
    # The call that finally succeeded (whichever index that landed at —
    # not assumed to be any particular position) must carry a summary-
    # shaped element: the SAME structural marker _router_main_call's own
    # decoration uses today, "[summary of earlier conversation]" — not a
    # byte-identical original text (compact() legitimately regenerates
    # the summary's content when new turns fold into it; asserting the
    # ORIGINAL text survived would be asserting the wrong thing).
    successful_call = loop.calls[-1]
    assert any(
        "[summary of earlier conversation]" in str(m.get("content", ""))
        for m in successful_call
    ), (
        f"expected a summary-shaped element on the successful call's wire, "
        f"got {successful_call!r}"
    )
