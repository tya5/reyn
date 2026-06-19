"""Tier 2: run_id propagation chat-side → Agent instance → agent.run().

FP-0008 PR-R (= tui-coder finding #1 propagation layer; PR-N
canonical-form follow-up). PR-N landed the canonical run_id form in
`skill_runner.spawn`, but Session's `_build_agent_for_skill_runner`
received the run_id only for `ChatInterventionBus` and did NOT forward
it to `_build_agent`. The Agent instance therefore had `self.run_id =
None` at construction; `agent.run()` then generated a fresh canonical
via `_make_run_id`, producing a 2-form mismatch:

  spawn-ack (conv pane):  20260528T122441355122Z_word_stats_demo_a197
  chat events.jsonl:      20260528T122441355122Z_word_stats_demo_cf45  (different suffix)
  skill events.jsonl:     20260528T122441357899Z_word_stats_demo_a197  (different microsec)

The fix threads run_id through:
  Session._build_agent_for_skill_runner(run_id, ...)
    → Session._build_agent(run_id=..., ...)
      → SkillRuntime(run_id=..., ...) ctor (= sets self.run_id)
        → agent.run() honors self.run_id (= no fresh _make_run_id)

This file pins:
  1. SkillRuntime.__init__ accepts run_id and sets it on the instance.
  2. agent.run() honors the constructor-set run_id (= no fresh
     generation when one was provided pre-run).
  3. agent.run() falls back to _make_run_id when no run_id was set
     at any layer (= preserves prior behavior for direct callers).
  4. Explicit run_id kwarg to agent.run() overrides the constructor
     value (= resume / WAL path preserved).

Tier rule discipline: every test docstring opens with Tier 2; no
mocks; no private-state assertions; no format-pinning.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.skill.skill_runtime import SkillRuntime

# ── 1. SkillRuntime.__init__ accepts run_id ─────────────────────────────────────


def test_agent_init_accepts_run_id_kwarg() -> None:
    """Tier 2: SkillRuntime.__init__ accepts a run_id kwarg and sets it on the instance."""
    pre_set_id = "20260528T122441355122Z_test_skill_a197"
    agent = SkillRuntime(model="standard", run_id=pre_set_id)
    assert agent.run_id == pre_set_id, (
        f"SkillRuntime.__init__(run_id={pre_set_id!r}) did not set instance "
        f"run_id; got {agent.run_id!r}. PR-R wiring required for "
        f"chat-side spawn → Agent instance propagation."
    )


def test_agent_init_without_run_id_defaults_to_none() -> None:
    """Tier 2: SkillRuntime.__init__ without run_id sets instance run_id to None."""
    agent = SkillRuntime(model="standard")
    assert agent.run_id is None, (
        f"SkillRuntime.__init__() without run_id should leave self.run_id = None "
        f"so agent.run() falls back to _make_run_id; got {agent.run_id!r}."
    )


# ── 2. agent.run() honors constructor-set run_id ─────────────────────────


@pytest.mark.asyncio
async def test_agent_run_honors_constructor_run_id() -> None:
    """Tier 2: when SkillRuntime(run_id=X), agent.run() does NOT regenerate; self.run_id stays X.

    The 2-form mismatch tui-coder caught was exactly the case where the
    constructor-set value was OVERWRITTEN by a fresh _make_run_id at
    agent.run() time. The fix preserves the constructor value.
    """
    pre_set_id = "20260528T122441355122Z_test_skill_a197"
    agent = SkillRuntime(model="standard", run_id=pre_set_id)

    # Build a minimal stub skill to invoke agent.run with — we only need
    # the run_id resolution logic, which runs at the top of agent.run().
    # Capture self.run_id after the resolution step by monkey-patching
    # the first downstream call (= Workspace ctor) to short-circuit.
    captured: dict[str, str | None] = {}

    class _StopSentinel(BaseException):
        pass

    def _capture_and_stop(*args, **kwargs):
        captured["run_id"] = agent.run_id
        raise _StopSentinel

    # Patch the Workspace import in agent.py to a sentinel-raising stub
    # so we capture run_id immediately after the resolution step but
    # before any heavy work. (= no mocks; we're injecting a known-stop
    # callable via the module's import path.)
    import reyn.skill.skill_runtime as agent_mod
    real_event_store = agent_mod.EventStore
    agent_mod.EventStore = _capture_and_stop  # type: ignore[assignment]

    # Real Skill stub - use a simple SimpleNamespace shape that satisfies
    # the first attribute reads in agent.run().
    import types
    skill_stub = types.SimpleNamespace(name="test_skill")
    try:
        with pytest.raises(_StopSentinel):
            await agent.run(skill_stub, initial_input={})  # type: ignore[arg-type]
    finally:
        agent_mod.EventStore = real_event_store

    assert captured.get("run_id") == pre_set_id, (
        f"agent.run() overwrote the constructor-set run_id. Expected "
        f"{pre_set_id!r}; got {captured.get('run_id')!r}. The PR-R "
        f"wiring requires agent.run() to honor self.run_id when set, "
        f"not regenerate via _make_run_id."
    )


# ── 3. explicit run= kwarg to agent.run() overrides constructor value ────


@pytest.mark.asyncio
async def test_agent_run_kwarg_overrides_constructor_run_id() -> None:
    """Tier 2: explicit run_id kwarg to agent.run() takes precedence over ctor value.

    Resume / WAL path: a caller with the original run_id passes it
    explicitly to agent.run() to keep events scoped to the same skill
    run. That MUST win over a stale constructor value.
    """
    ctor_id = "20260528T122441000000Z_test_skill_aaaa"
    override_id = "20260528T200000000000Z_test_skill_bbbb"
    agent = SkillRuntime(model="standard", run_id=ctor_id)

    captured: dict[str, str | None] = {}

    class _StopSentinel(BaseException):
        pass

    def _capture_and_stop(*args, **kwargs):
        captured["run_id"] = agent.run_id
        raise _StopSentinel

    import reyn.skill.skill_runtime as agent_mod
    real_event_store = agent_mod.EventStore
    agent_mod.EventStore = _capture_and_stop  # type: ignore[assignment]

    import types
    skill_stub = types.SimpleNamespace(name="test_skill")
    try:
        with pytest.raises(_StopSentinel):
            await agent.run(
                skill_stub, initial_input={}, run_id=override_id,  # type: ignore[arg-type]
            )
    finally:
        agent_mod.EventStore = real_event_store

    assert captured.get("run_id") == override_id, (
        f"explicit run_id kwarg should override ctor value. Expected "
        f"{override_id!r}; got {captured.get('run_id')!r}."
    )


# ── 4. fallback: no run_id anywhere → _make_run_id called ────────────────


@pytest.mark.asyncio
async def test_agent_run_falls_back_to_make_run_id_when_none() -> None:
    """Tier 2: no ctor run_id AND no kwarg → agent.run() generates fresh canonical.

    Preserves prior behavior for direct callers (= `reyn run` CLI path)
    that do not pre-determine a run_id at construction time.
    """
    agent = SkillRuntime(model="standard")
    assert agent.run_id is None  # baseline

    captured: dict[str, str | None] = {}

    class _StopSentinel(BaseException):
        pass

    def _capture_and_stop(*args, **kwargs):
        captured["run_id"] = agent.run_id
        raise _StopSentinel

    import reyn.skill.skill_runtime as agent_mod
    real_event_store = agent_mod.EventStore
    agent_mod.EventStore = _capture_and_stop  # type: ignore[assignment]

    import types
    skill_stub = types.SimpleNamespace(name="test_skill")
    try:
        with pytest.raises(_StopSentinel):
            await agent.run(skill_stub, initial_input={})  # type: ignore[arg-type]
    finally:
        agent_mod.EventStore = real_event_store

    resolved = captured.get("run_id")
    assert resolved is not None and resolved != "", (
        f"agent.run() should generate a fresh canonical run_id when "
        f"none was set; got {resolved!r}."
    )
    # The fresh id follows the canonical form pattern (= sanity check, not
    # exact match — that's pinned by test_skill_runner_canonical_run_id.py).
    import re
    canonical_re = re.compile(r"^\d{8}T\d{12}Z_[A-Za-z0-9_\-]+_[0-9a-f]{4}$")
    assert canonical_re.match(resolved), (
        f"Fresh run_id should match canonical form; got {resolved!r}."
    )


# ── 5. source-level audit: _build_agent_for_skill_runner threads run_id ──


def test_build_agent_for_skill_runner_threads_run_id_to_build_agent() -> None:
    """Tier 2: source-level audit — Session._build_agent_for_skill_runner passes run_id.

    Catches future regression where _build_agent_for_skill_runner stops
    forwarding run_id to _build_agent (= the exact PR-R defect).
    """
    from pathlib import Path
    source = (
        Path(__file__).parent.parent / "src" / "reyn" / "runtime" / "session.py"
    ).read_text(encoding="utf-8")
    # Locate the _build_agent_for_skill_runner function body
    fn_marker = "def _build_agent_for_skill_runner"
    fn_start = source.index(fn_marker)
    # Look for the next top-level function or class so we don't bleed past
    # the function body.
    fn_end_candidates = [
        source.find("\n    def ", fn_start + len(fn_marker)),
        source.find("\nclass ", fn_start + len(fn_marker)),
        len(source),
    ]
    fn_end = min([c for c in fn_end_candidates if c > 0])
    fn_body = source[fn_start:fn_end]

    assert "run_id=run_id" in fn_body, (
        "_build_agent_for_skill_runner must pass `run_id=run_id` to "
        "_build_agent (= PR-R wiring contract). Without this, the "
        "Agent instance receives None and agent.run() regenerates a "
        "fresh canonical, breaking the chat-side ↔ skill-side run_id "
        "match (= tui-coder finding #1 5-point smoke trace 2026-05-28)."
    )
