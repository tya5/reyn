"""Tier 2: OS-invariant tests for #1829 S1 — config-gated single-deployment Router.

S1 introduces an OFF-by-default gate that routes ``recorded_acompletion`` through a
single-deployment ``litellm.Router``, byte-equivalent to the direct
``litellm.acompletion`` call. These pin the S1 invariants:
  (a) the gate is OFF by default (production behavior unchanged).
  (b) the gate honors REYN_LLM_USE_ROUTER.
  (c) the single-deployment Router routes THROUGH ``litellm.acompletion`` — the
      make-or-break replay-compat invariant (LLMReplay monkeypatches that boundary;
      if Router bypassed it, every replay fixture would break).

Integration equivalence (the 6 router-replay fixtures stay green with the gate ON)
is verified by running ``test_replay_skill_router.py`` under REYN_LLM_USE_ROUTER=1.

Policy: no mocks (a real litellm.Router + a real spy on litellm.acompletion, not
unittest.mock); no private-state/count-pins; docstring Tier line.
"""
from __future__ import annotations

import litellm
import pytest

from reyn.llm.llm import _single_deployment_router, _use_llm_router


@pytest.fixture(autouse=True)
def _isolate_litellm_model_cost():
    """Test-isolation (#1829 S1, same class as #1762 global-state pollution):
    constructing a ``litellm.Router(model_list=[...])`` registers each deployment's
    model into the GLOBAL ``litellm.model_cost`` map. Without restoration that leaks
    across tests — e.g. test_session_cost_accumulation expects the proxy-prefixed
    model to be ABSENT (→ (None,None)); a leaked registration makes it resolve to
    cost 0.0 (the observed pytest-3.11 test-ordering failure). Snapshot + restore the
    map in place (mutate, don't rebind — other code holds the reference)."""
    before = dict(litellm.model_cost)
    yield
    litellm.model_cost.clear()
    litellm.model_cost.update(before)


def test_router_gate_off_by_default(monkeypatch) -> None:
    """Tier 2: the Router gate is OFF by default (production behavior unchanged)."""
    monkeypatch.delenv("REYN_LLM_USE_ROUTER", raising=False)
    assert _use_llm_router() is False, (
        "REYN_LLM_USE_ROUTER unset must mean Router OFF (direct litellm.acompletion)"
    )


def test_router_gate_honors_env(monkeypatch) -> None:
    """Tier 2: the gate honors REYN_LLM_USE_ROUTER truthy values."""
    for val in ("1", "true", "YES"):
        monkeypatch.setenv("REYN_LLM_USE_ROUTER", val)
        assert _use_llm_router() is True, f"REYN_LLM_USE_ROUTER={val!r} must enable Router"
    monkeypatch.setenv("REYN_LLM_USE_ROUTER", "0")
    assert _use_llm_router() is False


@pytest.mark.asyncio
async def test_single_deployment_router_routes_through_litellm_acompletion(monkeypatch) -> None:
    """Tier 2: the single-deployment Router invokes litellm.acompletion (replay-compat).

    The make-or-break invariant: LLMReplay monkeypatches litellm.acompletion, so the
    Router path must route through it for the replay fixtures to remain valid. A real
    spy (not a mock) on litellm.acompletion short-circuits before any network call.
    """
    fired: dict = {"v": False}
    orig = litellm.acompletion

    async def _spy(*_a, **_k):
        fired["v"] = True
        raise RuntimeError("spy-short-circuit")  # no real LLM call

    monkeypatch.setattr(litellm, "acompletion", _spy)
    router = _single_deployment_router("openai/gemini-2.5-flash-lite")
    try:
        await router.acompletion(
            model="openai/gemini-2.5-flash-lite",
            messages=[{"role": "user", "content": "hi"}],
        )
    except Exception:
        pass
    finally:
        monkeypatch.setattr(litellm, "acompletion", orig)
    assert fired["v"], (
        "Router.acompletion must route through litellm.acompletion (the LLMReplay "
        "monkeypatch boundary) — else every replay fixture breaks under the gate"
    )
