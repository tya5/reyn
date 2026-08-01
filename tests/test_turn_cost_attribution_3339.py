"""Tier 2: OS invariant — per-TURN token/cost attribution (#3339).

Per-call token/cost figures were produced correctly and then folded straight
into cumulative counters with no turn key, so a turn total was unrecoverable
afterwards — and could never be honestly reconstructed by differencing
cumulative counters. The fix threads the turn's ``chain_id`` (already minted
per user submission and carried by ``turn_started``/``turn_completed``) into
the cost path via an ambient turn scope, keys the ledger record with it, and
exposes a per-turn aggregate.

The invariants pinned here:

  1. Two turns aggregate SEPARATELY and correctly (a turn total is the sum of
     that turn's own calls, never a difference of running totals).
  2. A call made with NO turn in scope lands in NO turn bucket — while still
     being recorded in the cumulative counters (so the negative control is not
     vacuously satisfied by "nothing was recorded at all").
  3. The live per-turn buckets stay BOUNDED: the oldest turns are evicted and
     read as absent (never as a zero total), while the newest survives, and the
     cumulative counters are untouched. (#3283 ④ added a KEYED reader for older
     turns, so eviction is now observable to a real consumer — answered as
     "unknown", never as a 0; its own gates live in
     ``test_textual_chat_phase4_turn_cost_gutter_3283.py``.)
  4. The ledger row carries the turn key, witnessed through the real append
     path; a row written for a turnless call, and a pre-#3339 row, both stay
     valid.
  5. The ambient scope actually reaches the cost path: a turn bound at the
     session's router-loop seam attributes the LLM calls its turn makes.

Real ``BudgetTracker`` / ``BudgetLedger`` / ``TokenUsage`` / ``Session``
throughout — the LLM is the only faked collaborator (a scripted
``litellm.acompletion``, Tier 2c).
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import litellm

from reyn.core.turn_scope import active_turn, get_active_turn_chain_id
from reyn.llm.llm import recorded_acompletion
from reyn.llm.pricing import TokenUsage, UsageSource, estimate_cost
from reyn.runtime.budget.budget import (
    TURN_BUCKET_CAP,
    BudgetLedger,
    BudgetTracker,
    CostConfig,
)
from tests._support.agent_session import make_session

_MODEL = "gpt-4o"

# Deliberately non-default, mutually distinct per call, and non-round so a
# mis-attributed call cannot coincidentally produce the expected total.
_CALL_A1 = TokenUsage(prompt_tokens=1234, completion_tokens=567)
_CALL_A2 = TokenUsage(prompt_tokens=89, completion_tokens=21)
_CALL_B1 = TokenUsage(prompt_tokens=4321, completion_tokens=765)
_CALL_NO_TURN = TokenUsage(prompt_tokens=777, completion_tokens=333)


def _today_iso() -> str:
    """Local-time ISO-8601 stamp for TODAY (the ledger's own format) so a
    hand-written legacy row lands in the current day/month period."""
    lt = time.localtime(time.time())
    off = lt.tm_gmtoff
    sign = "+" if off >= 0 else "-"
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", lt)
        + f"{sign}{abs(off) // 3600:02d}:{(abs(off) % 3600) // 60:02d}"
    )


def _scripted_reply(prompt_tokens: int, completion_tokens: int):
    """A scripted ``litellm.acompletion`` returning one tool-free reply with
    the given usage — the LLM is the only collaborator these tests fake."""
    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        return SimpleNamespace(
            model=model,
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="こんにちは", tool_calls=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            ),
        )
    return _fake


def _cost(usage: TokenUsage) -> float:
    cost, _ = estimate_cost(_MODEL, usage)
    return cost or 0.0


def test_two_turns_aggregate_separately() -> None:
    """Tier 2: a turn's total is the sum of ITS OWN calls — turn A (2 calls)
    and turn B (1 call) aggregate independently, per the chain_id each call
    was recorded under."""
    tracker = BudgetTracker(CostConfig())
    tracker.record_llm(model=_MODEL, agent="alpha", usage=_CALL_A1, chain_id="turn-A")
    tracker.record_llm(model=_MODEL, agent="alpha", usage=_CALL_A2, chain_id="turn-A")
    tracker.record_llm(model=_MODEL, agent="alpha", usage=_CALL_B1, chain_id="turn-B")

    snap = tracker.snapshot()
    assert snap["turn_tokens"]["turn-A"] == _CALL_A1.total_tokens + _CALL_A2.total_tokens
    assert snap["turn_tokens"]["turn-B"] == _CALL_B1.total_tokens
    # Cost is priced per call at that call's own model rate, then summed —
    # so turn A's cost is its two calls' costs, not "session total minus B".
    assert snap["turn_cost_usd"]["turn-A"] == _cost(_CALL_A1) + _cost(_CALL_A2)
    assert snap["turn_cost_usd"]["turn-B"] == _cost(_CALL_B1)
    assert snap["turn_cost_usd"]["turn-A"] > 0.0, (
        f"{_MODEL} must be priced for this test to say anything about cost"
    )
    assert snap["last_turn_chain_id"] == "turn-B"
    # The latest-turn read answers about B (the turn that just recorded); a
    # turn that never ran is simply ABSENT, never reported as a zero total.
    assert tracker.latest_turn_usage() == {
        "chain_id": "turn-B",
        "tokens": _CALL_B1.total_tokens,
        # #3283 ④: the prompt/completion split is carried alongside the total
        # (the right gutter renders the two separately).
        "prompt_tokens": _CALL_B1.prompt_tokens,
        "completion_tokens": _CALL_B1.completion_tokens,
        "cost_usd": _cost(_CALL_B1),
        # #3351: provenance rides the same dict as the figures. These fixtures
        # build TokenUsage directly (no LLM call stated an origin), so the turn
        # reads UNKNOWN — never a falsely confident PROVIDER.
        "usage_source": UsageSource.UNKNOWN,
    }
    assert "turn-never" not in snap["turn_tokens"]


def test_call_outside_any_turn_contaminates_no_turn_bucket() -> None:
    """Tier 2: negative control — a call with no turn in scope is recorded in
    the cumulative counters but belongs to NO turn bucket — it neither joins
    the most recent turn nor creates a bucket of its own."""
    tracker = BudgetTracker(CostConfig())
    tracker.record_llm(model=_MODEL, agent="alpha", usage=_CALL_A1, chain_id="turn-A")
    before = tracker.latest_turn_usage()
    agent_tokens_before = tracker.agent_tokens("alpha")

    # No chain_id: a sub-agent / background / CLI call.
    tracker.record_llm(model=_MODEL, agent="alpha", usage=_CALL_NO_TURN)

    assert tracker.agent_tokens("alpha") == agent_tokens_before + _CALL_NO_TURN.total_tokens, (
        "the turnless call must still be RECORDED — otherwise this control is vacuous"
    )
    assert tracker.latest_turn_usage() == before, (
        "the turnless call must leave the latest turn's figures untouched"
    )
    snap = tracker.snapshot()
    assert list(snap["turn_tokens"]) == ["turn-A"], (
        "a turnless call must not create a bucket (incl. a None-keyed one)"
    )
    assert snap["last_turn_chain_id"] == "turn-A"


def test_turn_buckets_are_bounded_and_keep_the_newest() -> None:
    """Tier 2: the per-turn buckets are bounded — a long session evicts the
    OLDEST turns rather than accumulating one bucket per turn forever, and the
    turn that just recorded is never the victim.
    An evicted turn is ABSENT, not a zero total."""
    tracker = BudgetTracker(CostConfig())
    turns = [f"turn-{i:03d}" for i in range(TURN_BUCKET_CAP + 3)]
    for i, chain_id in enumerate(turns):
        # Distinct per-turn totals so an eviction cannot be masked by equality.
        tracker.record_llm(
            model=_MODEL, agent="alpha",
            usage=TokenUsage(prompt_tokens=10 + i, completion_tokens=1),
            chain_id=chain_id,
        )

    kept = tracker.snapshot()["turn_tokens"]
    assert len(kept) == TURN_BUCKET_CAP, "the bucket set must stay bounded"
    assert set(kept) == set(turns[3:]), "the oldest turns are the ones evicted"
    for chain_id in turns[:3]:
        assert chain_id not in kept, "an evicted turn is absent, never a 0 total"
    # The newest turn survives and still carries its own real figure.
    newest = turns[-1]
    _newest_usage = TokenUsage(prompt_tokens=10 + len(turns) - 1, completion_tokens=1)
    assert tracker.latest_turn_usage() == {
        "chain_id": newest,
        "tokens": _newest_usage.total_tokens,
        "prompt_tokens": _newest_usage.prompt_tokens,
        "completion_tokens": _newest_usage.completion_tokens,
        "cost_usd": _cost(_newest_usage),
        "usage_source": UsageSource.UNKNOWN,  # #3351: no origin stated by the fixture
    }
    # Cumulative counters are untouched by eviction — bounding the per-turn
    # view must not lose spend from the totals that enforce caps.
    assert tracker.agent_tokens("alpha") == sum(11 + i for i in range(len(turns)))


def test_ledger_row_carries_chain_id_through_the_real_append_path(tmp_path) -> None:
    """Tier 2: the turn key is durable — a record written by record_llm's own
    ledger append carries chain_id, and a turnless call's record omits it
    (rather than writing a null turn key)."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    tracker = BudgetTracker(CostConfig())
    tracker.hydrate(ledger_path)

    tracker.record_llm(model=_MODEL, agent="alpha", usage=_CALL_A1, chain_id="turn-A")
    tracker.record_llm(model=_MODEL, agent="alpha", usage=_CALL_NO_TURN)

    rows = list(BudgetLedger(ledger_path).iter_records())
    assert [r.get("chain_id") for r in rows] == ["turn-A", None]
    assert "chain_id" not in rows[1], "turnless call writes no turn key at all"
    assert rows[0]["tokens"] == _CALL_A1.total_tokens


def test_pre_3339_ledger_row_without_chain_id_still_hydrates(tmp_path) -> None:
    """Tier 2: an existing ledger written before the turn key existed stays
    readable — a missing chain_id is "no turn", never a parse failure."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        # A pre-#3339 record: ts/agent/model/tokens/cost_usd only.
        "ts": _today_iso(),
        "agent": "alpha",
        "model": _MODEL,
        "tokens": 4242,
        "cost_usd": 0.5,
    }
    ledger_path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    tracker = BudgetTracker(CostConfig())
    tracker.hydrate(ledger_path)

    snap = tracker.snapshot()
    assert snap["daily_tokens"] == 4242, "legacy row must hydrate, not be skipped"
    assert snap["turn_tokens"] == {}, "a row with no turn key joins no turn bucket"


def test_ambient_turn_scope_reaches_the_cost_chokepoint(monkeypatch) -> None:
    """Tier 2c: an LLM call made inside a turn scope is attributed to that turn
    by the ``recorded_acompletion`` chokepoint; the same call made outside any
    scope is attributed to no turn."""
    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(prompt_tokens=310, completion_tokens=57),
        )
    monkeypatch.setattr(litellm, "acompletion", _fake)

    tracker = BudgetTracker(CostConfig())

    async def _call() -> None:
        with active_turn("turn-Z"):
            await recorded_acompletion(
                model=_MODEL, messages=[{"role": "user", "content": "hi"}],
                purpose="main", recorder=tracker, agent="alpha",
            )
        assert get_active_turn_chain_id() is None, "scope must not outlive the turn"
        await recorded_acompletion(
            model=_MODEL, messages=[{"role": "user", "content": "hi"}],
            purpose="main", recorder=tracker, agent="alpha",
        )

    asyncio.run(_call())

    assert tracker.snapshot()["turn_tokens"] == {"turn-Z": 367}, (
        "the chokepoint must read the ambient turn key and file the in-scope call "
        "under it — and the out-of-scope call under no turn"
    )
    assert tracker.agent_tokens("alpha") == 734, "both calls recorded cumulatively"


def test_session_turn_attributes_its_llm_calls_to_its_chain_id(tmp_path, monkeypatch) -> None:
    """Tier 2c: end-to-end — a real Session turn binds its chain_id as the
    ambient turn scope, so the tokens/cost of the LLM call that turn makes are
    aggregated under that turn (and under no other)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(litellm, "acompletion", _scripted_reply(1500, 250))

    tracker = BudgetTracker(CostConfig())
    session = make_session(agent_name="test_agent", budget_tracker=tracker)

    asyncio.run(session._handle_inbox_text("hello", chain_id="turn-live-1"))

    latest = tracker.latest_turn_usage()
    assert latest is not None and latest["chain_id"] == "turn-live-1", (
        "the turn's own LLM call must be attributed to the turn's chain_id"
    )
    assert latest["tokens"] == 1750
    assert session.last_turn_usage == latest, (
        "the session surface must report its own turn's real figures"
    )


def test_no_turn_figure_is_published_as_unknown_never_zero(tmp_path, monkeypatch) -> None:
    """Tier 2: when there is no per-turn figure to report, the published
    tokens/cost are None — never a placeholder 0, which a renderer could not
    tell apart from a turn that genuinely cost nothing.

    The no-figure case goes through the real mechanism: a session that has not
    run a turn at all.

    #3283 ④ narrowed which cases these are. ``last_turn_usage`` now reads the
    KEYED ``BudgetTracker.turn_usage(chain_id)`` rather than "the latest turn
    process-wide", so another session's turn recording spend against the shared
    tracker no longer hides this session's own figure — asserted below, because
    that used to be an everyday unknown and is now a known."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(litellm, "acompletion", _scripted_reply(1500, 250))
    tracker = BudgetTracker(CostConfig())
    session = make_session(agent_name="test_agent", budget_tracker=tracker)

    # Same KEY SET as the success case, all values None — a consumer can read
    # any field without first branching on which case it got.
    unknown = {
        "chain_id": None, "tokens": None, "prompt_tokens": None,
        "completion_tokens": None, "cost_usd": None,
        # #3351: the provenance key is in the no-figure dict too (identical key
        # set), and None here means "no figure whose origin could be stated" —
        # distinct from UsageSource.UNKNOWN, a real figure of unstated origin.
        "usage_source": None,
    }
    assert session.last_turn_usage == unknown, "no turn run yet ⇒ no figure"

    # This session runs a real turn, so it DOES have a figure...
    asyncio.run(session._handle_inbox_text("hello", chain_id="turn-mine"))
    assert session.last_turn_usage["tokens"] == 1750

    # ...and it KEEPS it when some other session's turn becomes the tracker's
    # latest: the keyed read asks about this session's own chain_id, so it can
    # neither lose the figure nor return the other turn's number.
    tracker.record_llm(
        model=_MODEL, agent="beta", usage=_CALL_B1, chain_id="turn-elsewhere",
    )
    assert tracker.latest_turn_usage()["chain_id"] == "turn-elsewhere", (
        "fixture must actually move 'latest' off this session, or the "
        "assertion below is vacuous"
    )
    still_mine = session.last_turn_usage
    assert still_mine["chain_id"] == "turn-mine"
    assert still_mine["tokens"] == 1750
    assert still_mine["cost_usd"] is not None, (
        "the cost must be a real figure (this fixture's model is unpriced, so "
        "0.0 is correct) — never the None that means 'no figure'"
    )

    # The remaining unknown leg, through the real bounding mechanism: once this
    # session's own turn is EVICTED from the bounded buckets, the figure is gone
    # and must read as unknown — never as a 0.
    for i in range(TURN_BUCKET_CAP + 1):
        tracker.record_llm(
            model=_MODEL, agent="beta", usage=_CALL_B1, chain_id=f"turn-filler-{i}",
        )
    assert tracker.turn_usage("turn-mine") is None, (
        "fixture must actually evict this session's turn"
    )
    assert session.last_turn_usage == unknown
