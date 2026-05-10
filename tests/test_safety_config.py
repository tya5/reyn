"""Tier 2 invariants for FP-0004 — the unified ``safety:`` config namespace.

Pins the contract that:
  1. ``safety.loop.*`` and ``safety.timeout.*`` populate the user-facing
     ``ReynConfig.safety`` dataclass correctly.
  2. Defaults (absent ``safety:`` section) yield ``SafetyConfig()``.
  3. ``safety.loop.skill_calls_per_chain`` has ``CostLimitConfig`` shape.
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


# ─── 2. New safety: keys populate SafetyConfig ──────────────────────────


def test_safety_loop_keys_populate(
    isolated_project: Path,
) -> None:
    """Tier 2: ``safety.loop.*`` flows into ``SafetyConfig.loop``."""
    _write_yaml(isolated_project / "reyn.yaml", """
safety:
  loop:
    max_phase_visits: 50
    max_router_calls_per_turn: 7
    max_agent_hops: 5
    skill_calls_per_chain:
      hard_limit: 12
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    assert cfg.safety.loop.max_phase_visits == 50
    assert cfg.safety.loop.max_router_calls_per_turn == 7
    assert cfg.safety.loop.max_agent_hops == 5
    assert cfg.safety.loop.skill_calls_per_chain.hard_limit == 12.0


def test_safety_timeout_keys_populate(
    isolated_project: Path,
) -> None:
    """Tier 2: ``safety.timeout.*`` populates ``SafetyConfig.timeout``."""
    _write_yaml(isolated_project / "reyn.yaml", """
safety:
  timeout:
    llm_call_seconds: 120.0
    llm_max_retries: 5
    phase_seconds: 600.0
    chain_seconds: 300.0
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    assert cfg.safety.timeout.llm_call_seconds == 120.0
    assert cfg.safety.timeout.llm_max_retries == 5
    assert cfg.safety.timeout.phase_seconds == 600.0
    assert cfg.safety.timeout.chain_seconds == 300.0


# ─── 3. ``skill_calls_per_chain`` semantics ────────────────────────


def test_skill_calls_per_chain_default_is_unlimited(
    isolated_project: Path,
) -> None:
    """Tier 2: omitting ``skill_calls_per_chain`` yields a ``CostLimitConfig``
    with ``hard_limit=None`` (= unlimited).
    """
    _write_yaml(isolated_project / "reyn.yaml", """
safety:
  loop:
    max_phase_visits: 25
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    assert cfg.safety.loop.skill_calls_per_chain.hard_limit is None


def test_skill_calls_per_chain_preserves_other_fields(
    isolated_project: Path,
) -> None:
    """Tier 2: ``safety.loop.skill_calls_per_chain`` carries all
    ``CostLimitConfig`` sub-fields (warn_ratio, ask_on_exceed,
    extension_calls).
    """
    _write_yaml(isolated_project / "reyn.yaml", """
safety:
  loop:
    skill_calls_per_chain:
      hard_limit: 20
      warn_ratio: 0.5
      ask_on_exceed: true
      extension_calls: 7
""".lstrip())
    cfg = load_config(cwd=isolated_project)
    cap = cfg.safety.loop.skill_calls_per_chain
    assert cap.hard_limit == 20.0
    assert cap.warn_ratio == 0.5
    assert cap.ask_on_exceed is True
    assert cap.extension_calls == 7


# ─── 4. Exception hint_config_key surfaces correctly ──────────────────


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
