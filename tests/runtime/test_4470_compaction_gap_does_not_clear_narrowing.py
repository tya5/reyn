"""Tier 2: #4470 — a security hole #4468's own landing opened,
found by lead-coder before it shipped further: ``CompactionController``
builds candidates from ``self._history_access()`` (``lambda: self.history``,
#4387/#4468 RESIDENT-only), then sets ``covers_through_seq =
candidates[-1].seq`` — advancing the compaction watermark to the highest
seq it examined. If #4387's byte-driven eviction has already dropped an
entry whose ``prev_cover < seq <= candidates[-1].seq`` from residency
BEFORE this compaction pass ever ran, that entry is silently marked
"covered" (summarized) even though it was never seen by the summarizer at
all — the entries eviction removes are always a contiguous PREFIX
(eviction only ever removes from the front), so this is always a genuine
gap between ``prev_cover`` and the earliest resident seq, never a
false-positive on ordinary head/tail exclusion.

The security angle (lead-coder, severity:high): #4468's own untrusted-
content narrowing latch (``Session._max_evicted_untrusted_seq``) is
DELIBERATELY keyed to the compaction watermark, not to residency — correct
per #4468's own design (a resource-role eviction must not itself decide a
semantic-role question; only compaction, a semantic operation, may clear
the latch). This bug reopens exactly that hole from the OTHER side: a
compaction pass that never actually summarized the tainted entry still
advances the watermark past it, so the latch clears anyway — narrowing
lifts over content that was genuinely never folded away.

Fixed in ``compaction_controller.py``: when the entry immediately after
``prev_cover`` is missing from residency (a gap), SKIP this compaction
pass entirely rather than let ``covers_through_seq`` advance at all — a
stricter fix than an earlier draft (clamp ``prev_cover`` forward past the
gap and keep going), which still let the watermark become numerically
larger than the evicted-untrusted seq (the only thing #4468's latch check
compares against), reopening the hole even though that pass's own
candidates excluded the gap. The evicted-and-gapped range stays honestly
uncovered (safe — durable on disk, reachable again if reloaded) rather
than falsely marked done. Known, deliberate limitation: compaction cannot
make ANY progress while this specific gap persists (a functionality/
performance cost — more raw context reaches the LLM — never a security
cost); bridging the gap via ``_load_older_entries`` before compacting
(lead-coder's option 2) is left as a follow-up, not solved here.

Real ``Session`` + real ``CompactionController``/engine (only
``litellm.acompletion`` monkeypatched to a scripted summary, same
discipline ``tests/runtime/test_slash_compact_191.py`` already
established) + real narrowing config + real #4387 eviction (a small
``history_resident_config``, not manual ``self.history`` slicing) — this
is the "evict -> compaction -> narrowing has NOT lifted" test lead-coder's
dispatch asked for, reusing #4468's own control-arm discipline (assert
narrowed BEFORE compaction runs, so the post-compaction assertion has a
real baseline to compare against, not a vacuous "opt-in mechanism that was
never engaged" pass).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import litellm

from reyn.config import CompactionConfig
from reyn.config.chat import HistoryResidentConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.untrusted_narrowing import narrowing_on

# Mirrors test_slash_compact_191.py's own scripted summary — new_turn_seqs is
# cosmetic here (the controller derives covers_through_seq from candidates,
# not from the engine's own claim, unless the engine returns one explicitly;
# omitting it keeps the controller's own candidates[-1].seq fallback live,
# which is exactly the code path #4470 fixes).
_SUMMARY_JSON = json.dumps({
    "topic_arc": "compacted summary of older turns",
    "decisions": [], "pending": [],
    "session_user_facts": [], "artifacts_referenced": [],
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_session(tmp_path, monkeypatch, *, max_bytes: int) -> Session:
    """Same token-budget shape as test_slash_compact_191.py's own
    _make_session (head_budget≈74, tail_budget≈112 with t_max=2800,
    use_chars4_estimate=True, section_caps_spec_tokens=0), plus narrowing
    on and a #4387 resident-byte cap small enough to genuinely evict the
    earliest turns as padding accumulates."""
    import reyn.llm.model_budget as _mb

    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: 2800)
    return make_session(
        agent_name="default",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        compaction_config=CompactionConfig(
            use_chars4_estimate=True,
            section_caps_spec_tokens=0,
        ),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
        safety=narrowing_on(),
        history_resident_config=HistoryResidentConfig(max_bytes=max_bytes),
    )


def _script_compaction_llm(monkeypatch) -> None:
    async def _fake_acompletion(model, messages, **kw):  # noqa: ANN001, ANN003
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_SUMMARY_JSON))]
        )
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)


def _narrowed(s: Session) -> bool:
    return s._ephemeral_contextual_for_turn() is not None


def test_a_compaction_pass_that_never_saw_the_tainted_entry_does_not_clear_narrowing(
    tmp_path, monkeypatch,
):
    """Tier 2: the exact regression #4470 blocks on. A tainted entry is
    evicted (never summarized) before compaction ever runs, opening a gap
    between the compaction watermark (0, nothing compacted yet) and the
    earliest RESIDENT turn. Chosen fix (implementer's call, per lead-
    coder's explicit delegation): when that gap exists, compaction skips
    this pass ENTIRELY rather than let covers_through_seq advance past
    unseen territory (a stricter reading than clamping prev_cover forward
    to skip the gap and continuing — an earlier draft of this fix did
    that, and it still let the watermark become numerically larger than
    the evicted-untrusted seq, which is ALL #4468's own latch check
    compares against — so the hole reopened even though this pass's own
    candidates never included the gap). Narrowing must stay engaged, and
    the audit trail must show compaction explicitly recognised the gap
    rather than silently finding nothing to do for an unrelated reason.

    Known, deliberate limitation (documented, not silently assumed away):
    this means compaction cannot make ANY progress while this specific
    gap persists, until a future backward-hydrate re-fills it — a
    functionality/performance cost (more raw context sent to the LLM),
    never a security cost (no capability is regranted). Left as a
    follow-up rather than solved here (bridging the gap via
    ``_load_older_entries`` before compacting, lead-coder's option 2) —
    see the PR body for the explicit tradeoff record."""
    monkeypatch.chdir(tmp_path)
    # Small enough that the tainted entry + several padding turns are
    # genuinely evicted from residency once enough padding accumulates.
    s = _make_session(tmp_path, monkeypatch, max_bytes=20_000)
    _script_compaction_llm(monkeypatch)

    s._append_history(
        ChatMessage(
            role="user", content="<<<EXTERNAL>>> untrusted payload", ts=_now(),
            meta={"external_source": True},
        )
    )
    tainted_seq = s.history[-1].seq
    assert _narrowed(s), "control arm: the taint must engage narrowing before eviction"

    # Padding turns, same shape test_slash_compact_191.py uses to produce
    # real, non-empty candidates (head=2/tail=2 boundaries over 4000-char
    # turns) -- large enough that #4387's byte cap genuinely evicts the
    # tainted entry (and some early padding) from residency well before
    # compaction runs.
    for _ in range(8):
        s._append_history(ChatMessage(role="user", content="x" * 4000, ts=_now()))

    resident_seqs = [m.seq for m in s.history]
    assert tainted_seq not in resident_seqs, (
        "sanity: the tainted entry must have been genuinely EVICTED (not "
        "manually removed) before compaction runs, for this test to "
        "exercise the gap this dispatch is about -- if this fails, shrink "
        "max_bytes or add more padding"
    )
    assert resident_seqs[0] > 1, (
        "sanity: this test's own premise -- a gap must exist between the "
        "(never-yet-advanced) watermark of 0 and the earliest resident seq"
    )
    assert _narrowed(s), (
        "sanity: eviction alone (#4468's own latch) must still keep "
        "narrowing engaged at this point -- if this fails, #4468 itself "
        "regressed, not #4470's own fix"
    )

    result = asyncio.run(s._compact_now_for_op())

    assert result["summarized_turns"] == 0, (
        "compaction must have SKIPPED this pass (a gap exists right after "
        "the watermark) rather than silently advance covers_through_seq "
        "past content it never examined"
    )
    assert _narrowed(s), (
        "narrowing silently lifted after a compaction pass that NEVER "
        "actually saw the tainted entry (it was evicted, not summarized) "
        "-- the watermark must not advance past a gap it never examined"
    )
    assert not any(m.role == "summary" for m in s.history), (
        "no summary should have been produced at all -- this pass had "
        "nothing safe to cover"
    )


def test_compaction_still_clears_narrowing_once_it_genuinely_covers_the_tainted_seq(
    tmp_path, monkeypatch,
):
    """Tier 2: accept-side — #4470's clamp must not make narrowing
    permanently un-clearable. When the tainted entry IS still resident
    (no eviction has happened) and compaction genuinely folds it into a
    summary, narrowing must clear exactly as before."""
    monkeypatch.chdir(tmp_path)
    # A generous cap -- nothing gets evicted in this test at all, isolating
    # the "compaction genuinely covers it" case from #4470's own clamp.
    s = _make_session(tmp_path, monkeypatch, max_bytes=10_000_000)
    _script_compaction_llm(monkeypatch)

    s._append_history(
        ChatMessage(
            role="user", content="<<<EXTERNAL>>> untrusted payload", ts=_now(),
            meta={"external_source": True},
        )
    )
    tainted_seq = s.history[-1].seq

    for _ in range(8):
        s._append_history(ChatMessage(role="user", content="x" * 4000, ts=_now()))

    assert tainted_seq in [m.seq for m in s.history], (
        "sanity: nothing evicted in this test -- isolates the genuine-"
        "coverage case from #4470's own gap-clamp"
    )
    assert _narrowed(s), "sanity: still narrowed before compaction"

    result = asyncio.run(s._compact_now_for_op())
    assert result["summarized_turns"] > 0, "sanity: compaction must have run"

    assert not _narrowed(s), (
        "compaction that genuinely folds the tainted entry into a summary "
        "must still clear narrowing -- #4470's clamp must not make the "
        "latch permanently un-clearable"
    )
