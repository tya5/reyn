"""Tier 2: ADR-0025 sub-loop LLM call memoization.

Pins the contract:
  - args_hash is stable across identical (model, messages, tools, ...) inputs
  - SubLoopMemoProvider.record persists to PlanSnapshot.step_llm_calls
  - get_recorded_result returns the deserialised LLMToolCallResult on hit
  - Spill (>32KB serialised) writes to per-plan workspace file
  - reset_from_step deletes spilled LLM call records
  - Snapshot reload preserves the memo log
  - Drift (= different args_hash) misses cleanly

No real LLM. Provider tested directly against PlanRegistry; wiring
into planner.py is exercised in test_plan_resume_analyzer.py via the
step_llm_call_log forward.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.plan import (
    PlanRegistry,
    PlanSnapshot,
    SubLoopMemoProvider,
    compute_sub_loop_args_hash,
    extract_step_llm_call_records,
    plan_snapshot_path,
)
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage

# ── compute_sub_loop_args_hash ────────────────────────────────────────────


def test_args_hash_stable_across_identical_inputs() -> None:
    """Tier 2: same (model, messages, tools, tool_choice) → same hash."""
    args = dict(
        model="anthropic/claude-3-5-sonnet",
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"function": {"name": "search"}}],
        tool_choice="auto",
    )
    h1 = compute_sub_loop_args_hash(**args)
    h2 = compute_sub_loop_args_hash(**args)
    assert h1 == h2
    assert h1  # non-empty hash string


def test_args_hash_differs_on_message_change() -> None:
    """Tier 2: different messages → different hash."""
    base = dict(
        model="m", tools=None, tool_choice="auto",
    )
    h1 = compute_sub_loop_args_hash(
        messages=[{"role": "user", "content": "hello"}], **base,
    )
    h2 = compute_sub_loop_args_hash(
        messages=[{"role": "user", "content": "world"}], **base,
    )
    assert h1 != h2


def test_args_hash_differs_on_model_change() -> None:
    """Tier 2: different model → different hash (= prevents replaying
    one provider's response as another's)."""
    msgs = [{"role": "user", "content": "x"}]
    h1 = compute_sub_loop_args_hash(
        model="m1", messages=msgs, tools=None, tool_choice="auto",
    )
    h2 = compute_sub_loop_args_hash(
        model="m2", messages=msgs, tools=None, tool_choice="auto",
    )
    assert h1 != h2


# ── SubLoopMemoProvider record / get round-trip ──────────────────────────


def _sample_result(content: str = "ok", tool_calls: list | None = None) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason="stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
    )


@pytest.mark.asyncio
async def test_record_then_get_inline_round_trip(tmp_path: Path) -> None:
    """Tier 2: record a small result → get returns equivalent result."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    provider = SubLoopMemoProvider(
        plan_registry=reg, plan_id="p001", step_id="s1",
    )
    result = _sample_result(content="hello world")
    await provider.record(args_hash="abc123", result=result)

    got = provider.get_recorded_result("abc123")
    assert got is not None
    assert got.content == "hello world"
    assert got.finish_reason == "stop"
    assert got.usage.prompt_tokens == 10
    assert got.usage.completion_tokens == 5


@pytest.mark.asyncio
async def test_get_returns_none_on_miss(tmp_path: Path) -> None:
    """Tier 2: unknown args_hash → None (= caller does fresh call)."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    provider = SubLoopMemoProvider(
        plan_registry=reg, plan_id="p001", step_id="s1",
    )
    assert provider.get_recorded_result("nonexistent") is None


@pytest.mark.asyncio
async def test_record_persists_to_snapshot(tmp_path: Path) -> None:
    """Tier 2: record() saves the snapshot — survives reload."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    provider = SubLoopMemoProvider(
        plan_registry=reg, plan_id="p001", step_id="s1",
    )
    await provider.record(args_hash="h1", result=_sample_result("alpha"))
    await provider.record(args_hash="h2", result=_sample_result("beta"))

    # Reload snapshot from disk — confirm persistence.
    snap = PlanSnapshot.load("p001", plan_snapshot_path(tmp_path, "p001"))
    log = snap.step_llm_calls.get("s1") or []
    assert log
    assert log[0]["args_hash"] == "h1"
    assert log[1]["args_hash"] == "h2"


# ── spill (>32 KB) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_spills_large_result_to_file(tmp_path: Path) -> None:
    """Tier 2: result >32 KB serialised → spill to per-plan workspace
    file; snapshot holds a ref instead of inline JSON."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    provider = SubLoopMemoProvider(
        plan_registry=reg, plan_id="p001", step_id="s1",
    )
    huge = "X" * 60_000
    await provider.record(
        args_hash="h_big",
        result=_sample_result(content=huge),
    )

    snap = reg.get("p001")
    log = snap.step_llm_calls["s1"]
    assert log
    rec = log[0]
    assert rec["inline"] is None
    assert rec["ref"] is not None
    assert rec["ref"].startswith("step_llm_calls/s1/")

    # File on disk has the full serialised record.
    full = tmp_path / "plans" / "p001" / rec["ref"]
    assert full.exists()
    on_disk = json.loads(full.read_text(encoding="utf-8"))
    assert on_disk["content"] == huge


@pytest.mark.asyncio
async def test_get_resolves_spilled_record(tmp_path: Path) -> None:
    """Tier 2: get_recorded_result reads spilled file transparently."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    provider = SubLoopMemoProvider(
        plan_registry=reg, plan_id="p001", step_id="s1",
    )
    huge = "Y" * 50_000
    await provider.record(args_hash="hh", result=_sample_result(content=huge))

    # Re-build provider from seed records (simulate resume).
    log = reg.get("p001").step_llm_calls.get("s1") or []
    seed = extract_step_llm_call_records({"s1": log}, "s1")
    fresh_provider = SubLoopMemoProvider(
        plan_registry=reg, plan_id="p001", step_id="s1",
        seed_records=seed,
    )
    got = fresh_provider.get_recorded_result("hh")
    assert got is not None
    assert got.content == huge


@pytest.mark.asyncio
async def test_spill_file_missing_returns_none(tmp_path: Path) -> None:
    """Tier 2: ADR-0025 corruption fallback — spilled file gone →
    get returns None (= caller does fresh call). Mirrors ADR-0024 §4."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    provider = SubLoopMemoProvider(
        plan_registry=reg, plan_id="p001", step_id="s1",
    )
    huge = "Z" * 50_000
    await provider.record(args_hash="hh", result=_sample_result(content=huge))

    # Nuke the spilled file out from under us.
    snap = reg.get("p001")
    rec = snap.step_llm_calls["s1"][0]
    full = tmp_path / "plans" / "p001" / rec["ref"]
    full.unlink()

    # Fresh provider seeded from the (now-stale) snapshot data.
    seed = extract_step_llm_call_records(
        {"s1": list(snap.step_llm_calls["s1"])}, "s1",
    )
    fresh_provider = SubLoopMemoProvider(
        plan_registry=reg, plan_id="p001", step_id="s1",
        seed_records=seed,
    )
    assert fresh_provider.get_recorded_result("hh") is None


# ── reset_from_step interaction ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_from_step_clears_llm_call_log(tmp_path: Path) -> None:
    """Tier 2: PlanRegistry.reset_from_step clears step_llm_calls for
    cleared steps and unlinks any spilled records."""
    reg = PlanRegistry(agent_name="default", agent_state_dir=tmp_path)
    reg.start(plan_id="p001", chain_id="c1", goal="g", applied_seq=10)
    p1 = SubLoopMemoProvider(
        plan_registry=reg, plan_id="p001", step_id="s1",
    )
    p2 = SubLoopMemoProvider(
        plan_registry=reg, plan_id="p001", step_id="s2",
    )
    await p1.record(args_hash="h1", result=_sample_result("a"))
    await p2.record(args_hash="h2", result=_sample_result(content="X" * 50_000))

    # s2 is spilled; verify file exists.
    snap = reg.get("p001")
    spilled_ref = snap.step_llm_calls["s2"][0]["ref"]
    spilled_path = tmp_path / "plans" / "p001" / spilled_ref
    assert spilled_path.exists()

    reg.reset_from_step(
        plan_id="p001", from_step_id="s2", step_order=["s1", "s2"],
    )

    snap2 = reg.get("p001")
    # s1 preserved, s2 cleared.
    assert "s1" in snap2.step_llm_calls
    assert "s2" not in snap2.step_llm_calls
    # Spilled file unlinked.
    assert not spilled_path.exists()


# ── extract_step_llm_call_records ────────────────────────────────────────


def test_extract_records_returns_empty_for_unknown_step() -> None:
    """Tier 2: extract on a step with no log returns empty list."""
    records = extract_step_llm_call_records({}, "s1")
    assert records == []


def test_extract_records_skips_malformed_entries() -> None:
    """Tier 2: entries without args_hash are filtered (= defensive
    against future schema drift)."""
    log = {
        "s1": [
            {"args_hash": "ok", "inline": {}, "ref": None},
            {"no_hash": True},          # bad
            "not a dict",                # bad
            {"args_hash": "also_ok", "inline": None, "ref": "s1/0.json"},
        ]
    }
    records = extract_step_llm_call_records(log, "s1")
    assert records
    assert records[0].args_hash == "ok"
    assert records[1].args_hash == "also_ok"


# ── analyzer forward ─────────────────────────────────────────────────────


def test_analyzer_forwards_step_llm_call_log_to_resume_plan(tmp_path: Path) -> None:
    """Tier 2: PlanResumeAnalyzer.analyze populates
    PlanResumePlan.step_llm_call_log from snapshot.step_llm_calls."""
    from reyn.core.plan import PlanResumeAnalyzer
    from reyn.runtime.planner import Plan, PlanStep

    snap = PlanSnapshot.empty(
        plan_id="p001", agent_name="default", chain_id="c0", goal="g",
    )
    snap.step_llm_calls["s1"] = [
        {"args_hash": "h1", "inline": {"content": "ok"}, "ref": None,
         "usage": {}},
    ]
    plan = Plan(
        goal="g",
        steps=(PlanStep("s1", "first", ()), PlanStep("s2", "second", ())),
    )

    rp = PlanResumeAnalyzer().analyze(
        snapshot=snap, decomposition=plan, wal_events=[],
    )
    assert "s1" in rp.step_llm_call_log
    assert rp.step_llm_call_log["s1"][0]["args_hash"] == "h1"
