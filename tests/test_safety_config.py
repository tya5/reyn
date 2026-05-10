"""Tier 2 invariants for FP-0004 — the unified ``safety:`` config namespace.

Pins the contract that:
  1. ``safety.loop.*`` and ``safety.timeout.*`` populate the user-facing
     ``ReynConfig.safety`` dataclass and ALSO back-fill the legacy
     ``limits`` / ``multi_agent`` / ``cost.*`` dataclasses so existing
     reference sites keep working.
  2. When both new (``safety.*``) and legacy (``limits.*`` / ``multi_agent.*`` /
     ``cost.router_invocations_per_turn`` / ``cost.per_chain_skill_calls.hard_limit``)
     keys are set, the new key wins.
  3. When only the legacy keys are set, the loader yields byte-identical
     dataclasses to pre-FP-0004 behaviour.
  4. The ``hint_config_key`` attribute on each safety-related exception
     names the new key the operator should adjust.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import (
    LoopConfig,
    SafetyConfig,
    TimeoutConfig,
    load_config,
)


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fresh project root with no parent reyn.yaml leakage.

    ``load_config`` walks up to find ``reyn.yaml``; without isolation
    it would discover the repo's own reyn.yaml. Pointing HOME at tmp_path
    + writing reyn.yaml in tmp_path gives us a self-contained run.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# ─── 1. Defaults / empty config ─────────────────────────────────────────


def test_default_safety_config_matches_dataclass_defaults(
    isolated_project: Path,
) -> None:
    """Tier 2: an empty ``safety:`` (= absent) yields default
    ``SafetyConfig``. No surprise migration when the user has not
    written any keys.
    """
    _write_yaml(isolated_project / "reyn.yaml", "model: standard\n")
    cfg = load_config(cwd=isolated_project)
    assert cfg.safety == SafetyConfig()
    assert cfg.safety.loop == LoopConfig()
    assert cfg.safety.timeout == TimeoutConfig()


# ─── 2. New safety: keys populate SafetyConfig + back-fill legacy ──────


def test_safety_loop_keys_populate_and_backfill(
    isolated_project: Path,
) -> None:
    """Tier 2: ``safety.loop.*`` flows into ``SafetyConfig.loop`` AND
    back-fills the legacy dataclasses (``limits.phase.max_visits``,
    ``multi_agent.max_hop_depth``, ``cost.router_invocations_per_turn``,
    ``cost.per_chain_skill_calls.hard_limit``).
    """
    _write_yaml(isolated_project / "reyn.yaml", """
safety:
  loop:
    max_phase_visits: 50
    max_router_calls_per_turn: 7
    max_agent_hops: 5
    max_skill_calls_per_chain: 12
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    # New surface
    assert cfg.safety.loop.max_phase_visits == 50
    assert cfg.safety.loop.max_router_calls_per_turn == 7
    assert cfg.safety.loop.max_agent_hops == 5
    assert cfg.safety.loop.max_skill_calls_per_chain == 12
    # Legacy back-fill
    assert cfg.limits.phase.max_visits == 50
    assert cfg.cost.router_invocations_per_turn == 7
    assert cfg.multi_agent.max_hop_depth == 5
    assert cfg.cost.per_chain_skill_calls.hard_limit == 12.0


def test_safety_timeout_keys_populate_and_backfill(
    isolated_project: Path,
) -> None:
    """Tier 2: ``safety.timeout.*`` populates ``SafetyConfig.timeout``
    and back-fills ``limits.llm.*`` / ``limits.phase.max_wall_seconds``
    / ``multi_agent.chain_timeout_seconds``.
    """
    _write_yaml(isolated_project / "reyn.yaml", """
safety:
  timeout:
    llm_call_seconds: 120.0
    llm_max_retries: 5
    phase_seconds: 600.0
    chain_seconds: 300.0
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    # New surface
    assert cfg.safety.timeout.llm_call_seconds == 120.0
    assert cfg.safety.timeout.llm_max_retries == 5
    assert cfg.safety.timeout.phase_seconds == 600.0
    assert cfg.safety.timeout.chain_seconds == 300.0
    # Legacy back-fill
    assert cfg.limits.llm.timeout == 120.0
    assert cfg.limits.llm.max_retries == 5
    assert cfg.limits.phase.max_wall_seconds == 600.0
    assert cfg.multi_agent.chain_timeout_seconds == 300.0


# ─── 3. Legacy-only configs unchanged ─────────────────────────────────


def test_legacy_only_config_unchanged(isolated_project: Path) -> None:
    """Tier 2: when no ``safety:`` is written, legacy keys are honoured
    exactly as they were pre-FP-0004 — back-compat invariant.
    """
    _write_yaml(isolated_project / "reyn.yaml", """
limits:
  phase:
    max_visits: 33
    max_wall_seconds: 90.0
  llm:
    timeout: 45.0
    max_retries: 2
multi_agent:
  max_hop_depth: 4
  chain_timeout_seconds: 90.0
cost:
  router_invocations_per_turn: 6
  per_chain_skill_calls:
    hard_limit: 15
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    assert cfg.limits.phase.max_visits == 33
    assert cfg.limits.phase.max_wall_seconds == 90.0
    assert cfg.limits.llm.timeout == 45.0
    assert cfg.limits.llm.max_retries == 2
    assert cfg.multi_agent.max_hop_depth == 4
    assert cfg.multi_agent.chain_timeout_seconds == 90.0
    assert cfg.cost.router_invocations_per_turn == 6
    assert cfg.cost.per_chain_skill_calls.hard_limit == 15.0
    # ``safety`` stays at its dataclass defaults — it does NOT get
    # back-populated from legacy keys (= legacy paths stay legacy).
    assert cfg.safety == SafetyConfig()


# ─── 4. Conflict resolution: new wins ─────────────────────────────────


def test_safety_overrides_legacy_when_both_set(isolated_project: Path) -> None:
    """Tier 2: when both ``safety.loop.max_phase_visits`` and
    ``limits.phase.max_visits`` are set, the new key wins.
    """
    _write_yaml(isolated_project / "reyn.yaml", """
limits:
  phase:
    max_visits: 10        # legacy
safety:
  loop:
    max_phase_visits: 99  # new — should win
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    assert cfg.limits.phase.max_visits == 99
    assert cfg.safety.loop.max_phase_visits == 99


def test_partial_safety_does_not_drop_legacy(isolated_project: Path) -> None:
    """Tier 2: writing ``safety.loop.max_phase_visits`` alone must not
    overwrite legacy keys for OTHER axes (e.g. llm timeout). The
    deprecation reader checks per-axis presence, not per-section.
    """
    _write_yaml(isolated_project / "reyn.yaml", """
limits:
  llm:
    timeout: 45.0     # legacy, should stay
safety:
  loop:
    max_phase_visits: 77
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    assert cfg.limits.phase.max_visits == 77      # new
    assert cfg.limits.llm.timeout == 45.0         # untouched legacy


# ─── 5. ``max_skill_calls_per_chain`` semantics ────────────────────────


def test_max_skill_calls_per_chain_default_is_unlimited(
    isolated_project: Path,
) -> None:
    """Tier 2: omitting ``max_skill_calls_per_chain`` (or setting it to
    null) yields ``None`` (= unlimited) and DOES NOT override an
    existing ``cost.per_chain_skill_calls.hard_limit``.
    """
    _write_yaml(isolated_project / "reyn.yaml", """
cost:
  per_chain_skill_calls:
    hard_limit: 5
safety:
  loop:
    max_phase_visits: 25  # touch a different field; do not set max_skill_calls_per_chain
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    assert cfg.safety.loop.max_skill_calls_per_chain is None
    # Because the new key was NOT explicitly set, the legacy hard_limit stays.
    assert cfg.cost.per_chain_skill_calls.hard_limit == 5.0


def test_max_skill_calls_preserves_other_cost_limit_fields(
    isolated_project: Path,
) -> None:
    """Tier 2: when ``safety.loop.max_skill_calls_per_chain`` overrides
    ``cost.per_chain_skill_calls.hard_limit``, the other fields
    (``ask_on_exceed`` / ``extension_calls`` / ``warn_ratio``) are
    preserved.
    """
    _write_yaml(isolated_project / "reyn.yaml", """
cost:
  per_chain_skill_calls:
    hard_limit: 3
    warn_ratio: 0.5
    ask_on_exceed: true
    extension_calls: 7
safety:
  loop:
    max_skill_calls_per_chain: 20
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    cap = cfg.cost.per_chain_skill_calls
    assert cap.hard_limit == 20.0
    assert cap.warn_ratio == 0.5
    assert cap.ask_on_exceed is True
    assert cap.extension_calls == 7


# ─── 6. Exception hint_config_key surfaces correctly ──────────────────


def test_loop_limit_exception_carries_hint_key() -> None:
    """Tier 2: ``LoopLimitExceededError.hint_config_key`` is the
    user-facing config key surfaced in error messages.
    """
    from reyn.kernel.runtime import LoopLimitExceededError

    assert LoopLimitExceededError.hint_config_key == "safety.loop.max_phase_visits"


def test_phase_budget_exception_carries_hint_key() -> None:
    """Tier 2: ``PhaseBudgetExceededError.hint_config_key`` names the
    timeout knob.
    """
    from reyn.kernel.runtime import PhaseBudgetExceededError

    assert PhaseBudgetExceededError.hint_config_key == "safety.timeout.phase_seconds"
    # And the message includes the hint.
    exc = PhaseBudgetExceededError(phase="p1", elapsed=120.0, budget=60.0)
    assert "safety.timeout.phase_seconds" in str(exc)


def test_router_cap_exception_carries_hint_key() -> None:
    """Tier 2: ``RouterCapExceeded.hint_config_key`` names the loop knob
    and is embedded in the message.
    """
    from reyn.chat.session import RouterCapExceeded

    assert RouterCapExceeded.hint_config_key == "safety.loop.max_router_calls_per_turn"
    exc = RouterCapExceeded(count=4, cap=3, last_reason="")
    assert "safety.loop.max_router_calls_per_turn" in str(exc)
