"""Tier 2: chat-axis turn_budget activation (#1092 PR-F1, ADDITIVE).

F1 wires the chat axis to the SHARED turn_budget service (the C2 payoff): the
Session builds a TurnBudgetEngine off the RESOLVED model (#1172-safe) and
hands it to RouterHostAdapter, which exposes ``wrap_up_output_reserve`` — the
``output_reserve`` that RouterLoop._force_close_call will pass as ``max_tokens``
to hard-cap the chat handoff's consolidation (the F2 by-construction floor).

ADDITIVE / inert: chat never calls ``_force_close_call`` until the F2 handoff
lands, so this changes no chat behaviour on its own. And chat deliberately
exposes ONLY the reserve, NOT ``should_force_close`` — chat is REACTIVE-only (a
proactive mid-turn force-close would truncate a live conversation; a phase, being
task execution, proactively wraps up — a deliberate per-axis architectural
choice, not a missing trigger).

No mocks: real engines, a real ModelResolver, a real Session.
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.services.turn_budget import (
    DEFAULT_WRAP_UP_OUTPUT_RESERVE_TOKENS,
    build_default_turn_budget_engine,
    try_build_default_turn_budget_engine,
)
from tests.test_router_host_adapter_invariants import _make_adapter

# ── adapter property ─────────────────────────────────────────────────────────


def test_adapter_exposes_reserve_when_engine_present() -> None:
    """Tier 2: with a turn_budget engine, wrap_up_output_reserve ==
    engine.budget.output_reserve (the cap RouterLoop._force_close_call applies)."""
    eng = build_default_turn_budget_engine("gpt-4o-mini", use_chars4=True)
    adapter = _make_adapter(turn_budget_engine=eng)
    assert adapter.wrap_up_output_reserve == eng.budget.output_reserve
    assert adapter.wrap_up_output_reserve == DEFAULT_WRAP_UP_OUTPUT_RESERVE_TOKENS


def test_adapter_reserve_none_without_engine() -> None:
    """Tier 2: no engine (legacy / test paths) → None → no cap (== the
    pre-PR-F chat behaviour; the cap only engages once an engine is wired)."""
    adapter = _make_adapter()
    assert adapter.wrap_up_output_reserve is None


def test_adapter_does_not_expose_proactive_trigger() -> None:
    """Tier 2: chat is REACTIVE-only — the adapter exposes wrap_up_output_reserve
    (the wrap-up cap) but NOT should_force_close (the proactive trigger). This is
    the deliberate per-axis choice: phase force-closes proactively, chat only at
    the F2 floor-exhausted terminal."""
    adapter = _make_adapter(
        turn_budget_engine=build_default_turn_budget_engine("gpt-4o-mini", use_chars4=True)
    )
    assert not hasattr(adapter, "should_force_close")


# ── session-level wiring (the F1 change: resolve model + build + pass) ─────────


def test_chat_session_activates_turn_budget_via_resolved_model(tmp_path: Path) -> None:
    """Tier 2: a real Session builds the chat turn_budget engine off the
    RESOLVED model (#1172-safe — never the cosmetic class) and its router host
    exposes a non-None, asserted reserve. Exercises the session wiring (resolve
    self.model + build_default_turn_budget_engine + pass to the adapter), via the
    public ``session.router_host`` surface."""
    session = Session(
        agent_name="f1",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    reserve = session.router_host.wrap_up_output_reserve
    assert reserve is not None                                  # activated
    assert reserve == DEFAULT_WRAP_UP_OUTPUT_RESERVE_TOKENS     # shared default


# ── graceful degradation: small-context model cannot support force-close ───────


def test_try_build_returns_none_for_small_context_model(monkeypatch) -> None:
    """Tier 2: a model whose context is too small for the by-construction floor
    (output_reserve + offload_cap < threshold) yields None — NOT a raise. The
    model genuinely cannot support force-close; the caller degrades."""
    monkeypatch.setattr(
        "reyn.llm.model_budget.get_max_input_tokens", lambda model, **kw: 2000
    )
    assert try_build_default_turn_budget_engine("tiny", use_chars4=True) is None


def test_try_build_returns_engine_for_large_context_model() -> None:
    """Tier 2: a normal large-context model yields a real engine (the floor holds)."""
    eng = try_build_default_turn_budget_engine("gpt-4o-mini", use_chars4=True)
    assert eng is not None
    assert eng.budget.output_reserve == DEFAULT_WRAP_UP_OUTPUT_RESERVE_TOKENS


def test_chat_session_on_small_model_constructs_without_force_close(
    tmp_path: Path, monkeypatch
) -> None:
    """Tier 2: a Session on a too-small-context model STILL CONSTRUCTS (no
    __init__ raise) and exposes reserve=None — force-close is simply unavailable,
    chat falls back to the pre-force-close path. Regression guard: building the
    engine unconditionally would assert-fail here and break every small-model
    chat session."""
    monkeypatch.setattr(
        "reyn.llm.model_budget.get_max_input_tokens", lambda model, **kw: 2000
    )
    session = Session(
        agent_name="f1-small",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    assert session.router_host.wrap_up_output_reserve is None  # degraded, no crash
