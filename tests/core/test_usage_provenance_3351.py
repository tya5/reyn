"""Tier 2 / Tier 1: #3351 — an ESTIMATED token count is recorded as such, and
the turns it was recorded for are identifiable afterwards.

#3349 removed the systematic cause of estimates leaking into accounting (a
capability gate meant ``stream_options={"include_usage": True}`` was never sent
for the default-config providers). The FALLBACK itself remains: when a
provider's stream carries no usage at all, ``litellm.stream_chunk_builder``
still fills the counts from ``litellm.token_counter``, and that number reaches
``record_llm`` → ``/cost`` → the budget caps. The defect these tests pin is not
that the estimate exists — it is deliberately kept, a number beats no number —
but that its ORIGIN used to disappear, leaving a provider figure and a local
estimate indistinguishable in every durable record.

Both paths are driven through the REAL chokepoint with a real
``BudgetTracker`` + a real on-disk ledger + a real ``EventLog``; the only
stand-in is ``litellm.acompletion`` itself, a scripted async callable (never a
``unittest.mock`` double) whose two variants differ in exactly one respect:
whether the stream carries the provider's usage chunk. Sentinel provider counts
(7777 / 131, unreachable by any estimate of the two-token prompt) keep the
provider leg from passing on a numerically-close estimate.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import litellm
import pytest
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices, Usage

from reyn.core.events.event_schema import EVENT_AUDIT_REQUIREMENTS
from reyn.core.events.events import EventLog, set_llm_request_event_log
from reyn.core.turn_scope import active_turn
from reyn.llm.llm import call_llm_tools, recorded_acompletion
from reyn.llm.pricing import TokenUsage, UsageSource, parse_usage_source
from reyn.runtime.budget.budget import BudgetLedger, BudgetTracker, CostConfig
from tests._support.events import collect_events

# Unreachable by any token_counter estimate of the tiny prompt below, so the
# provider leg cannot pass on an estimate that happens to land nearby.
_PROVIDER_PROMPT_TOKENS = 7777
_PROVIDER_COMPLETION_TOKENS = 131
_CONTENT = "hello world"
_MESSAGES = [{"role": "user", "content": "hi"}]
_MODEL = "gemini/gemini-2.5-flash-lite"


def _make_streaming_acompletion(*, provider_reports_usage: bool):
    """Scripted stand-in for ``litellm.acompletion`` — a real async callable.

    ``provider_reports_usage=False`` is the residual #3351 path: reyn asks for
    ``include_usage`` (that is #3349's unconditional flag) and the provider
    streams no usage anyway, so litellm's reconstruction has nothing to sum and
    falls back to ``litellm.token_counter``. The two variants differ ONLY in
    that final chunk, so any difference the tests observe is attributable to
    the provider's usage payload and nothing else.
    """

    async def _acompletion(model: str, messages: list, **kw: Any) -> Any:
        def _chunk(delta: Delta, finish_reason: "str | None" = None) -> ModelResponseStream:
            return ModelResponseStream(
                id="resp-3351", created=1, model=model, object="chat.completion.chunk",
                choices=[StreamingChoices(index=0, delta=delta, finish_reason=finish_reason)],
            )

        async def _gen():
            yield _chunk(Delta(role="assistant", content=_CONTENT))
            yield _chunk(Delta(), finish_reason="stop")
            if provider_reports_usage:
                usage_chunk = _chunk(Delta())
                usage_chunk.usage = Usage(
                    prompt_tokens=_PROVIDER_PROMPT_TOKENS,
                    completion_tokens=_PROVIDER_COMPLETION_TOKENS,
                    total_tokens=_PROVIDER_PROMPT_TOKENS + _PROVIDER_COMPLETION_TOKENS,
                )
                yield usage_chunk

        return _gen()

    return _acompletion


def _ledger_rows(path) -> list[dict]:
    return list(BudgetLedger(path).iter_records())


@pytest.fixture
def _reset_event_log():
    yield
    set_llm_request_event_log(None)


def _run_turn(tracker: BudgetTracker, chain_id: str) -> Any:
    with active_turn(chain_id):
        return asyncio.run(recorded_acompletion(
            model=_MODEL, messages=_MESSAGES, purpose="main",
            model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
            recorder=tracker, agent="alpha", emit_cost_events=True,
        ))


def test_an_estimated_turn_is_identifiable_from_the_durable_ledger(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: a call whose counts came from ``litellm.token_counter`` lands in
    the ledger marked ``estimated`` and keyed by its turn — so "which turns were
    billed on estimated counts" is answerable after the fact, from the same file
    the caps are rebuilt from.

    The estimate is NOT suppressed: the figure still flows into the counters.
    What changed is that the record says what kind of figure it is."""
    monkeypatch.setattr(
        litellm, "acompletion", _make_streaming_acompletion(provider_reports_usage=False),
    )
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    tracker = BudgetTracker(CostConfig())
    tracker.hydrate(ledger_path)

    response = _run_turn(tracker, "turn-estimated")

    # The estimate really did fire, and it really is a different number from the
    # provider sentinels — otherwise this test would be witnessing nothing.
    assert response.usage.prompt_tokens not in (0, _PROVIDER_PROMPT_TOKENS)
    assert tracker.agent_tokens("alpha") > 0, (
        "the estimate must still be RECORDED — #3351 is about provenance, not suppression"
    )

    (row,) = _ledger_rows(ledger_path)
    assert parse_usage_source(row["usage_source"]) is UsageSource.ESTIMATED
    assert row["chain_id"] == "turn-estimated", (
        "the estimated call must be traceable to its turn, or the audit question "
        "'which turns were estimated' stays unanswerable"
    )
    assert row["tokens"] == response.usage.total_tokens


def test_a_provider_reported_turn_is_recorded_as_provider(monkeypatch, tmp_path) -> None:
    """Tier 2: the other leg, distinguished by sentinel values — an identical
    call whose stream DOES carry the provider's usage records those exact counts
    and marks them ``provider``. Without this leg the estimated verdict above
    could be produced by a marker that is simply always ``estimated``."""
    monkeypatch.setattr(
        litellm, "acompletion", _make_streaming_acompletion(provider_reports_usage=True),
    )
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    tracker = BudgetTracker(CostConfig())
    tracker.hydrate(ledger_path)

    _run_turn(tracker, "turn-provider")

    (row,) = _ledger_rows(ledger_path)
    assert parse_usage_source(row["usage_source"]) is UsageSource.PROVIDER
    assert row["tokens"] == _PROVIDER_PROMPT_TOKENS + _PROVIDER_COMPLETION_TOKENS
    assert tracker.agent_tokens("alpha") == (
        _PROVIDER_PROMPT_TOKENS + _PROVIDER_COMPLETION_TOKENS
    )


def test_the_per_turn_read_carries_provenance_with_the_figures(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: the live per-turn read publishes provenance in the SAME dict as
    the token figures, and one estimated call contaminates the turn's verdict.

    A turn that mixes a provider-reported call with an estimated one reads
    ``estimated`` — its total contains an estimate, and an auditor of that total
    is entitled to know. Both orderings are asserted, so a merge degraded to
    "whichever call recorded last" cannot pass; the provider-only turn is the
    control that keeps the verdict from being "always estimated"."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    tracker = BudgetTracker(CostConfig())
    tracker.hydrate(ledger_path)

    def _provider(reports: bool) -> None:
        monkeypatch.setattr(
            litellm, "acompletion", _make_streaming_acompletion(provider_reports_usage=reports),
        )

    _provider(True)
    _run_turn(tracker, "turn-clean")
    _run_turn(tracker, "turn-mixed")      # provider first…
    _provider(False)
    _run_turn(tracker, "turn-mixed")      # …then estimated
    _run_turn(tracker, "turn-mixed-rev")  # estimated first…
    _provider(True)
    _run_turn(tracker, "turn-mixed-rev")  # …then provider

    clean = tracker.turn_usage("turn-clean")
    mixed = tracker.turn_usage("turn-mixed")
    mixed_rev = tracker.turn_usage("turn-mixed-rev")
    assert clean["usage_source"] is UsageSource.PROVIDER
    assert mixed["usage_source"] is UsageSource.ESTIMATED
    assert mixed_rev["usage_source"] is UsageSource.ESTIMATED, (
        "an estimated call must contaminate the turn regardless of when it ran"
    )
    assert mixed["tokens"] > clean["tokens"], (
        "the mixed turn must really have accumulated both calls, or its verdict "
        "says nothing about merging"
    )


def test_the_cost_audit_event_reports_where_the_numbers_came_from(
    monkeypatch, _reset_event_log,
) -> None:
    """Tier 2: the ``llm_response_received`` audit-event carries the figures'
    provenance next to the figures, for both origins, alongside the turn key —
    so ``reyn events`` can single out the estimated turns of a past run without
    the ledger.

    Also asserts the event satisfies every field ``EVENT_AUDIT_REQUIREMENTS``
    declares for it, so a future emitter that drops one is caught here."""
    required = EVENT_AUDIT_REQUIREMENTS["llm_response_received"]
    tracker = BudgetTracker(CostConfig())
    seen: dict[str, dict] = {}

    for chain_id, reports_usage in (("turn-p", True), ("turn-e", False)):
        monkeypatch.setattr(
            litellm, "acompletion",
            _make_streaming_acompletion(provider_reports_usage=reports_usage),
        )
        log = EventLog()
        collected = collect_events(log)
        set_llm_request_event_log(log)
        _run_turn(tracker, chain_id)
        event = next(e for e in collected if e.type == "llm_response_received")
        payload = json.loads(json.dumps(event.data))  # as an audit reader sees it
        missing = required - set(payload)
        assert not missing, f"llm_response_received is missing declared audit fields: {missing}"
        seen[chain_id] = payload

    assert seen["turn-p"]["usage_source"] == UsageSource.PROVIDER.value
    assert seen["turn-p"]["prompt_tokens"] == _PROVIDER_PROMPT_TOKENS
    assert seen["turn-e"]["usage_source"] == UsageSource.ESTIMATED.value
    assert seen["turn-e"]["prompt_tokens"] != _PROVIDER_PROMPT_TOKENS
    assert seen["turn-e"]["chain_id"] == "turn-e", (
        "the estimated event must name its turn — that is what makes the turn "
        "findable in the audit trail after the fact"
    )


def test_call_id_and_finish_reason_land_on_the_response_event_only(
    monkeypatch, _reset_event_log,
) -> None:
    """Tier 2: #4691 Phase 1 ① — ``call_id``/``finish_reason`` are the litellm
    response's own ``id``/``choices[0].finish_reason``, stamped on
    ``llm_response_received`` (measured off the reconstructed response, not
    invented) and deliberately ABSENT from ``llm_called`` (which fires before
    the response exists, so it has no response fields to report)."""
    tracker = BudgetTracker(CostConfig())
    monkeypatch.setattr(
        litellm, "acompletion",
        _make_streaming_acompletion(provider_reports_usage=True),
    )
    log = EventLog()
    collected = collect_events(log)
    set_llm_request_event_log(log)
    _run_turn(tracker, "turn-callid")

    called = next(e for e in collected if e.type == "llm_called")
    received = next(e for e in collected if e.type == "llm_response_received")
    received_payload = json.loads(json.dumps(received.data))

    # The scripted stand-in's chunks all carry id="resp-3351" and the content
    # chunk's own finish_reason="stop" (see _make_streaming_acompletion) —
    # litellm's stream reconstruction carries both onto the rebuilt response.
    assert received_payload["call_id"] == "resp-3351"
    assert received_payload["finish_reason"] == "stop"
    assert "call_id" not in called.data, (
        "llm_called fires before the response exists — it must not carry a "
        "response-only field"
    )
    assert "finish_reason" not in called.data


def test_provenance_survives_the_call_llm_tools_re_extraction(monkeypatch) -> None:
    """Tier 2: ``call_llm_tools`` — the entry point ``RouterLoop`` uses for every
    chat turn — re-reads usage from the response object it got back from the
    chokepoint and does its OWN ``budget.record_llm``. Provenance must survive
    that second extraction, or the path that feeds `/cost` in production records
    estimates as unattributed numbers while the chokepoint's own path is fine."""
    monkeypatch.setattr(
        litellm, "acompletion", _make_streaming_acompletion(provider_reports_usage=False),
    )
    tracker = BudgetTracker(CostConfig())

    with active_turn("turn-tools"):
        asyncio.run(call_llm_tools(
            model=_MODEL, messages=_MESSAGES, tools=[],
            budget=tracker, budget_agent="alpha",
        ))

    turn = tracker.turn_usage("turn-tools")
    assert turn["tokens"] > 0, "the call must have been recorded at all"
    assert turn["usage_source"] is UsageSource.ESTIMATED


def test_a_non_streamed_call_records_the_provider_as_the_origin(monkeypatch, tmp_path) -> None:
    """Tier 2: the whole-collect (non-streaming) leg states ``provider``.

    ``o1-pro`` is a model litellm reports as unable to stream natively, so this
    call takes the branch that never touches ``stream_chunk_builder`` — the only
    place litellm synthesizes counts for a chat completion. Without a witness
    here the non-streaming path could silently record every call as ``unknown``
    while the streaming tests above stayed green, and `/cost` would report
    "origin not stated" for the majority of production calls."""
    async def _whole_response(model: str, messages: list, **kw: Any) -> Any:
        assert not kw.get("stream"), "this leg must not be a streamed call"
        return litellm.ModelResponse(
            id="resp-3351", created=1, model=model, object="chat.completion",
            choices=[{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": _CONTENT},
            }],
            usage={
                "prompt_tokens": _PROVIDER_PROMPT_TOKENS,
                "completion_tokens": _PROVIDER_COMPLETION_TOKENS,
                "total_tokens": _PROVIDER_PROMPT_TOKENS + _PROVIDER_COMPLETION_TOKENS,
            },
        )

    monkeypatch.setattr(litellm, "acompletion", _whole_response)
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    tracker = BudgetTracker(CostConfig())
    tracker.hydrate(ledger_path)

    with active_turn("turn-whole"):
        asyncio.run(recorded_acompletion(
            model="o1-pro", messages=_MESSAGES, purpose="main",
            model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
            recorder=tracker, agent="alpha",
        ))

    (row,) = _ledger_rows(ledger_path)
    assert parse_usage_source(row["usage_source"]) is UsageSource.PROVIDER
    assert row["tokens"] == _PROVIDER_PROMPT_TOKENS + _PROVIDER_COMPLETION_TOKENS


def test_a_pre_3351_ledger_row_reads_as_unknown_never_as_provider(tmp_path) -> None:
    """Tier 2: a ledger written before provenance existed stays readable, and
    its records read as ``unknown`` — never as provider-verified.

    The direction is the whole point: an absent field is exactly the case that
    must not acquire a claim it never made. Hydration over the mixed ledger must
    also still work, so old rows keep enforcing the caps they always did."""
    ledger_path = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "ts": "2026-05-02T10:23:00+09:00", "agent": "alpha",
        "model": _MODEL, "tokens": 300, "cost_usd": 0.001,
    }
    ledger_path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    tracker = BudgetTracker(CostConfig())
    tracker.hydrate(ledger_path)
    tracker.record_llm(
        model=_MODEL, agent="alpha",
        usage=TokenUsage(prompt_tokens=11, completion_tokens=2, source=UsageSource.ESTIMATED),
    )

    rows = _ledger_rows(ledger_path)
    assert [parse_usage_source(r.get("usage_source")) for r in rows] == [
        UsageSource.UNKNOWN, UsageSource.ESTIMATED,
    ]
    assert "usage_source" not in rows[0], "the legacy record must be left as it was"
    assert tracker.agent_tokens("alpha") >= 300, (
        "the legacy row must still hydrate into the cap counters"
    )


def test_summing_usage_keeps_the_least_confident_provenance() -> None:
    """Tier 1: ``TokenUsage`` arithmetic — provenance travels with the numbers.

    A sum is ``provider`` only when every contribution was; one estimated part
    makes the total estimated. An EMPTY usage (the accumulator every aggregation
    in the runtime starts from) states nothing and must not drag a run of
    provider-reported calls down to ``unknown``."""
    provider = TokenUsage(prompt_tokens=7777, completion_tokens=131,
                          source=UsageSource.PROVIDER)
    estimated = TokenUsage(prompt_tokens=13, completion_tokens=2,
                           source=UsageSource.ESTIMATED)
    unstated = TokenUsage(prompt_tokens=5, completion_tokens=1)

    assert (provider + provider).source is UsageSource.PROVIDER
    assert (provider + estimated).source is UsageSource.ESTIMATED
    assert (estimated + provider).source is UsageSource.ESTIMATED
    assert (provider + unstated).source is UsageSource.UNKNOWN
    assert (TokenUsage() + provider).source is UsageSource.PROVIDER

    total = TokenUsage()
    total += provider
    total += estimated
    assert total.source is UsageSource.ESTIMATED
    assert total.total_tokens == provider.total_tokens + estimated.total_tokens

    # Enumerated from the enum itself, not from a hand-listed subset: a variant
    # added later without a place in the merge ordering fails here rather than
    # raising from inside a cost path at runtime.
    for member in UsageSource:
        same = TokenUsage(prompt_tokens=1, source=member)
        assert (same + same).source is member


def test_a_serialized_usage_round_trips_its_provenance() -> None:
    """Tier 1: ``to_dict`` / ``from_dict`` preserve a NON-DEFAULT provenance,
    and anything unreadable — a missing field, a null, a variant this build does
    not know — reads back as ``unknown`` rather than as ``provider``."""
    for source in (UsageSource.PROVIDER, UsageSource.ESTIMATED, UsageSource.UNKNOWN):
        original = TokenUsage(prompt_tokens=7777, completion_tokens=131, source=source)
        assert TokenUsage.from_dict(original.to_dict()).source is source

    assert TokenUsage.from_dict({"prompt_tokens": 3}).source is UsageSource.UNKNOWN
    assert TokenUsage.from_dict({"usage_source": None}).source is UsageSource.UNKNOWN
    assert TokenUsage.from_dict({"usage_source": "from-the-future"}).source is (
        UsageSource.UNKNOWN
    )
