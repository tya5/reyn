"""Tier 2 invariants for FP-0003 (= per-chain budget extension via ask_user).

Pins the contract that:
  1. ``ask_on_exceed: false`` (default) preserves prior hard-refuse behaviour.
  2. ``extend_chain_calls`` raises the effective cap by ``additional`` and
     subsequent ``check_pre_spawn`` calls allow that many more spawns.
  3. ``check_pre_spawn`` surfaces ``ask_on_exceed`` + ``extension_calls`` in
     ``BudgetCheck.context`` so the chat layer can decide whether to dispatch
     the user-approval flow without re-reading the config.
  4. Negative / zero ``additional`` is treated as a no-op (= no change to
     the effective cap, no exception).

These are pure tracker-level tests — no chat session, no asyncio. The
session.py integration (= ask_user dispatch + extend on approval) is
covered by the session invariants suite once it lands.
"""
from __future__ import annotations

from reyn.budget.budget import (
    BudgetTracker,
    CostConfig,
    CostLimitConfig,
)


def _make_tracker(
    *, hard: int, ask_on_exceed: bool = False, extension_calls: int = 0
) -> BudgetTracker:
    return BudgetTracker(
        CostConfig(
            per_chain_skill_calls=CostLimitConfig(
                hard_limit=float(hard),
                warn_ratio=0.8,
                ask_on_exceed=ask_on_exceed,
                extension_calls=extension_calls,
            ),
        ),
    )


# ─── 1. Default (ask_on_exceed=False) preserves prior behaviour ──────────


def test_default_no_ask_on_exceed_in_context() -> None:
    """Tier 2: when ``ask_on_exceed`` is False (default), the refusal
    context still carries the flag (= False) so the chat layer's
    `if check.context.get('ask_on_exceed')` short-circuits without any
    additional logic on the existing path.
    """
    t = _make_tracker(hard=1)
    # First spawn allowed
    chk = t.check_pre_spawn(chain_id="c1", skill="s")
    assert chk.allowed, chk
    t.record_spawn(chain_id="c1", skill="s")
    # Second spawn refused
    chk = t.check_pre_spawn(chain_id="c1", skill="s")
    assert not chk.allowed
    assert chk.context.get("ask_on_exceed") is False
    assert chk.context.get("extension_calls") == 0
    assert chk.context.get("base_hard") == 1
    assert chk.context.get("extensions_granted") == 0
    assert chk.context.get("hard") == 1


# ─── 2. ask_on_exceed surfaces the flag for the chat layer ──────────────


def test_ask_on_exceed_flag_surfaces_in_refusal_context() -> None:
    """Tier 2: when ``ask_on_exceed: true`` + ``extension_calls > 0`` are
    configured, the refusal's ``BudgetCheck.context`` includes both so
    the chat layer can dispatch ask_user without re-reading config.
    """
    t = _make_tracker(hard=1, ask_on_exceed=True, extension_calls=3)
    # Hit the cap
    t.record_spawn(chain_id="c1", skill="s")
    chk = t.check_pre_spawn(chain_id="c1", skill="s")
    assert not chk.allowed
    assert chk.context["ask_on_exceed"] is True
    assert chk.context["extension_calls"] == 3
    assert chk.context["base_hard"] == 1
    assert chk.context["extensions_granted"] == 0


# ─── 3. extend_chain_calls raises the effective cap ─────────────────────


def test_extend_chain_calls_raises_effective_cap() -> None:
    """Tier 2: after ``extend_chain_calls(additional=2)`` is invoked,
    ``check_pre_spawn`` allows 2 more spawns before refusing again.
    """
    t = _make_tracker(hard=1, ask_on_exceed=True, extension_calls=2)
    # Hit the cap
    t.record_spawn(chain_id="c1", skill="s")
    assert not t.check_pre_spawn(chain_id="c1", skill="s").allowed
    # Extend
    new_total = t.extend_chain_calls(
        chain_id="c1", skill="s", additional=2,
    )
    assert new_total == 2
    # Two more spawns allowed
    assert t.check_pre_spawn(chain_id="c1", skill="s").allowed
    t.record_spawn(chain_id="c1", skill="s")
    assert t.check_pre_spawn(chain_id="c1", skill="s").allowed
    t.record_spawn(chain_id="c1", skill="s")
    # Third post-extension spawn refused
    chk = t.check_pre_spawn(chain_id="c1", skill="s")
    assert not chk.allowed
    assert chk.context["base_hard"] == 1
    assert chk.context["extensions_granted"] == 2
    assert chk.context["hard"] == 3


def test_extend_chain_calls_isolated_per_chain() -> None:
    """Tier 2: extending chain c1's cap does not affect chain c2's cap
    even when the same skill is involved. Per-(chain, skill) bookkeeping
    is preserved (= the proposal explicitly requires this).
    """
    t = _make_tracker(hard=1, ask_on_exceed=True, extension_calls=3)
    t.record_spawn(chain_id="c1", skill="s")
    t.record_spawn(chain_id="c2", skill="s")
    # Extend only c1
    t.extend_chain_calls(chain_id="c1", skill="s", additional=3)
    # c1 allows; c2 still refused
    assert t.check_pre_spawn(chain_id="c1", skill="s").allowed
    assert not t.check_pre_spawn(chain_id="c2", skill="s").allowed


def test_extend_chain_calls_isolated_per_skill() -> None:
    """Tier 2: extending (c1, s1)'s cap does not affect (c1, s2). The
    extension is keyed on (chain_id, skill), not chain_id alone.
    """
    t = _make_tracker(hard=1, ask_on_exceed=True, extension_calls=3)
    t.record_spawn(chain_id="c1", skill="s1")
    t.record_spawn(chain_id="c1", skill="s2")
    t.extend_chain_calls(chain_id="c1", skill="s1", additional=3)
    assert t.check_pre_spawn(chain_id="c1", skill="s1").allowed
    assert not t.check_pre_spawn(chain_id="c1", skill="s2").allowed


# ─── 4. Negative / zero additional is a no-op ───────────────────────────


def test_extend_chain_calls_zero_additional_is_noop() -> None:
    """Tier 2: ``additional=0`` returns the current extension total
    without raising; the refusal state is unchanged.
    """
    t = _make_tracker(hard=1, ask_on_exceed=True, extension_calls=3)
    t.record_spawn(chain_id="c1", skill="s")
    new_total = t.extend_chain_calls(chain_id="c1", skill="s", additional=0)
    assert new_total == 0
    assert not t.check_pre_spawn(chain_id="c1", skill="s").allowed


def test_extend_chain_calls_negative_additional_is_noop() -> None:
    """Tier 2: negative ``additional`` is silently treated as zero;
    we do not allow callers to *shrink* a granted extension.
    """
    t = _make_tracker(hard=1, ask_on_exceed=True, extension_calls=3)
    t.extend_chain_calls(chain_id="c1", skill="s", additional=2)
    new_total = t.extend_chain_calls(
        chain_id="c1", skill="s", additional=-5,
    )
    assert new_total == 2  # unchanged


# ─── 5. Cumulative extensions stack ─────────────────────────────────────


def test_extend_chain_calls_cumulative() -> None:
    """Tier 2: multiple ``extend_chain_calls`` calls stack. The user can
    approve repeated extensions over the lifetime of a chain.
    """
    t = _make_tracker(hard=1, ask_on_exceed=True, extension_calls=2)
    t.record_spawn(chain_id="c1", skill="s")
    t.extend_chain_calls(chain_id="c1", skill="s", additional=2)
    # Use the 2 extra spawns
    t.record_spawn(chain_id="c1", skill="s")
    t.record_spawn(chain_id="c1", skill="s")
    assert not t.check_pre_spawn(chain_id="c1", skill="s").allowed
    # Approve another extension
    new_total = t.extend_chain_calls(
        chain_id="c1", skill="s", additional=2,
    )
    assert new_total == 4
    chk = t.check_pre_spawn(chain_id="c1", skill="s")
    assert chk.allowed
    assert chk.context["extensions_granted"] == 4
    assert chk.context["hard"] == 5  # base 1 + extensions 4
