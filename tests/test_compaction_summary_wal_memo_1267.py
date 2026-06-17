"""Tier 2: #1267 — the phase compaction summary is WAL-memoized (shared-layer fix).

The compaction summary LLM call (`compact_control_ir_results` via `recorded_acompletion`)
was not memoized, so a compaction×resume re-summarized non-deterministically → the
downstream act-turn memo drifted → crash-resume MISS → op re-execution (idempotency
hole). The fix memo-wraps the summary (content args_hash, `recorded_acompletion`
unchanged): on resume the recorded summary is reused (HIT, no re-summarize).

This pins the core with a CHANGING-summary test (the only meaningful shape — a fixed
scripted summary would mask the gap): the summariser returns a different value on each
call, so a memo-HIT (reusing the first value) is distinguishable from a re-summarize
(the second value).

- `test_compaction_summary_memo_hit_skips_resummarize`: unit-level, path-agnostic (the
  shared `compact_control_ir_results`). MISS → calls the summariser + records; HIT →
  reuses the recorded summary, NO second summariser call.
- `test_compaction_summary_no_memo_resummarizes` (falsification control): without the
  memo seam the second call re-summarizes (the gap the fix closes).

Both json-mode `_run_act_loop` and the converged `_run_routerloop_op_loop` call this
SAME `compact_control_ir_results` with the SAME `summary_memo` (wired in PhaseExecutor),
so the shared-function behavior pinned here is the behavior on both phase paths; the
end-to-end converged crash-resume is exercised by the resume-memo suite.
"""
from __future__ import annotations

import asyncio

import litellm

from reyn.config import CompactionConfig, PhaseActResultsCompactionConfig
from reyn.core.events.events import EventLog
from reyn.services.compaction.engine import CompactionEngine, compact_control_ir_results


class _Resp:
    def __init__(self, text: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})(), "finish_reason": "stop"})()]
        self.usage = None


def _changing_summary(counter: dict):
    """litellm.acompletion stub returning a DIFFERENT summary per call (V1, V2, ...)
    and counting calls — so a memo-HIT (reuse V1) is distinguishable from a
    re-summarize (V2)."""
    async def _ac(model, messages, **kw):  # noqa: ANN001, ANN003
        counter["n"] += 1
        return _Resp(f"COMPACTED_SUMMARY_V{counter['n']}")
    return _ac


def _engine() -> CompactionEngine:
    return CompactionEngine(
        model="gpt-3.5-turbo", events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True), T_SP=0,
    )


def _cfg() -> PhaseActResultsCompactionConfig:
    return PhaseActResultsCompactionConfig(
        use_chars4_estimate=True, recent_act_turns_raw=1,
        summarize_older_threshold_tokens=1,  # any non-empty older slice exceeds it
    )


# A dict-backed summary memo (the duck-typed seam: lookup_summary / record_summary).
# Mirrors LLMCallRecorder._SummaryMemo's interface; the real WAL-backed one is
# exercised end-to-end by the converged resume-memo suite.
class _DictSummaryMemo:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def lookup_summary(self, phase, args_hash):  # noqa: ANN001
        return self.store.get(args_hash)

    async def record_summary(self, phase, args_hash, summary):  # noqa: ANN001
        self.store[args_hash] = summary


_OLDER = [{"kind": "file", "op": "read", "content": "fixture content " * 30} for _ in range(3)]


def test_compaction_summary_memo_hit_skips_resummarize(monkeypatch) -> None:
    """Tier 2: with a summary_memo, the SECOND compaction of the same inputs HITS the
    recorded summary — the summariser is NOT called again (no re-summarize), and the
    summary is the FIRST value (V1), not the changed second value (V2). This is the
    #1267 fix: a compaction×resume reuses the recorded summary deterministically."""
    counter = {"n": 0}
    monkeypatch.setattr(litellm, "acompletion", _changing_summary(counter))
    memo = _DictSummaryMemo()
    engine, cfg = _engine(), _cfg()

    # Run 1 (fresh): MISS → summariser called (V1) → recorded.
    r1 = asyncio.run(compact_control_ir_results(
        list(_OLDER), engine=engine, cfg=cfg, events=EventLog(), phase="draft", summary_memo=memo,
    ))
    assert counter["n"] == 1, f"run1 must call the summariser once; got {counter['n']}"
    assert r1 and r1[0].get("kind") == "__compacted_phase_results__"
    v1 = r1[0]["summary"]
    assert "V1" in v1, f"run1 summary should be V1; got {v1!r}"

    # Run 2 (resume — same inputs): HIT → summariser NOT called again, reuse V1.
    r2 = asyncio.run(compact_control_ir_results(
        list(_OLDER), engine=engine, cfg=cfg, events=EventLog(), phase="draft", summary_memo=memo,
    ))
    assert counter["n"] == 1, (
        "the compaction summary must memo-HIT on the second (resume) compaction — the "
        f"summariser must NOT be re-called; got {counter['n']} calls (re-summarized)"
    )
    assert r2[0]["summary"] == v1, (
        f"resume must reuse the recorded summary (V1), not the changed V2; got {r2[0]['summary']!r}"
    )


def test_compaction_summary_no_memo_resummarizes(monkeypatch) -> None:
    """Tier 2: falsification control — WITHOUT the summary_memo seam, the second
    compaction RE-summarizes (calls the summariser again → V2), the non-deterministic
    drift the #1267 fix closes."""
    counter = {"n": 0}
    monkeypatch.setattr(litellm, "acompletion", _changing_summary(counter))
    engine, cfg = _engine(), _cfg()

    r1 = asyncio.run(compact_control_ir_results(
        list(_OLDER), engine=engine, cfg=cfg, events=EventLog(), phase="draft", summary_memo=None,
    ))
    r2 = asyncio.run(compact_control_ir_results(
        list(_OLDER), engine=engine, cfg=cfg, events=EventLog(), phase="draft", summary_memo=None,
    ))
    assert counter["n"] == 2, (
        f"without the memo the summariser is re-called each time; got {counter['n']}"
    )
    assert "V1" in r1[0]["summary"] and "V2" in r2[0]["summary"], (
        "without the memo the resume gets a DIFFERENT (re-summarized) value — the "
        f"drift #1267 fixes; got r1={r1[0]['summary']!r} r2={r2[0]['summary']!r}"
    )
