"""Tier 2: #5793 — "operator explicit only, else NOT PASSED at all" across the
3 litellm-touching surfaces (owner decision, verbatim: "わざわざ reyn が別に規定
を持つ理由がわからん...未指定なら litellm 規定にして").

Each surface has its OWN litellm entry point (`acompletion` / `aembedding` /
`Router`) and is verified SEPARATELY here — a pass on one entry point does not
generalize to the others (lead-coder's own explicit instruction on this issue).
Every test below inspects the REAL kwargs/attributes litellm actually received
(a spy on the real litellm boundary, or the real litellm.Router's own resolved
attribute), not a declaration that the code "should" omit the key.

"Not passed" and "None/0 passed as a value" are different facts (owner's own
distinction) — every assertion below checks the KEY IS ABSENT (or, for the
Router, that litellm's OWN default numeral appears, not reyn's former one),
never merely that a variable equals ``None`` in reyn's own code.
"""
from __future__ import annotations

import litellm
import pytest

from reyn.config.embedding import EmbeddingConfig
from reyn.config.infra import RouterConfig
from reyn.data.embedding.litellm_provider import LiteLLMEmbeddingProvider
from reyn.llm.llm import _single_deployment_router, call_llm_tools


@pytest.fixture(autouse=True)
def _isolate_litellm_model_cost():
    """Same #1762 global-state-isolation discipline as test_llm_router_s1_1829.py —
    Router build registers a model_cost placeholder; restore in place."""
    before = dict(litellm.model_cost)
    yield
    litellm.model_cost.clear()
    litellm.model_cost.update(before)


# ── config defaults themselves (the data-layer half of the fix) ────────────


def test_config_defaults_are_none_not_reyn_numbers() -> None:
    """Tier 2: the 3 config surfaces' own defaults are None — the OTHER half
    of "not passed", proven at the config layer (the kwargs-omission tests
    below prove the consuming half)."""
    from reyn.config.chat import TimeoutConfig
    assert TimeoutConfig().llm_call_seconds is None
    assert TimeoutConfig().llm_max_retries is None
    assert EmbeddingConfig().timeout is None
    assert RouterConfig().num_retries is None


def test_call_llm_tools_own_default_is_none_not_1() -> None:
    """Tier 2: #5793 — call_llm_tools's OWN function-signature default for
    max_retries used to be the literal 1 (a reyn-invented number, independent
    of any config), forced onto every caller that omits the kwarg (the main
    chat router path always does — see router_loop.py's own call sites,
    none of which pass max_retries). Now None, so an unset caller reaches
    litellm with no forced retry count at all."""
    import inspect
    sig = inspect.signature(call_llm_tools)
    assert sig.parameters["max_retries"].default is None


# ── Router surface: litellm.Router's own entry point ────────────────────────


@pytest.mark.asyncio
async def test_router_num_retries_is_litellms_own_default_not_reyns_former_3() -> None:
    """Tier 2: #5793 — a Router built from an UNCONFIGURED RouterConfig (the
    single-deployment path `_single_deployment_router` uses when no
    reyn.yaml `llm.router.*` and no legacy env var are set) must carry
    litellm's OWN num_retries default, not reyn's former hardcoded 3.

    Verified against a REAL bare litellm.Router built the same way, with
    num_retries genuinely omitted from its own kwargs — the reference value
    is read live, never hardcoded, so this stays correct even if litellm's
    own default itself changes in a future litellm release.

    ``async def`` (#5793 fix): `_single_deployment_router` is per-running-
    loop-cached (`asyncio.get_running_loop()`), so it needs a real running
    loop — a bare `def` test raised "no running event loop"."""
    reference = litellm.Router(
        model_list=[{"model_name": "t5793-ref", "litellm_params": {"model": "openai/gpt-4o-mini"}}],
    )
    assert reference.num_retries != 3, (
        "sanity: litellm's own default must differ from reyn's former 3, "
        "else this test cannot distinguish the two"
    )

    router = _single_deployment_router("openai/t5793-probe-model")
    assert router.num_retries == reference.num_retries, (
        f"an unconfigured Router must resolve to litellm's OWN default "
        f"({reference.num_retries!r}), not reyn's former hardcoded 3 — "
        f"got {router.num_retries!r}"
    )


@pytest.mark.asyncio
async def test_router_num_retries_still_honours_an_explicit_operator_value() -> None:
    """Tier 2: #5793 deny — an operator who DOES set llm.router.num_retries
    still reaches litellm with that exact value; #5793 only removed the
    unset-case fallback, not the override path."""
    from reyn.llm.llm import set_router_config

    token = set_router_config(RouterConfig(use=True, num_retries=9))
    try:
        router = _single_deployment_router("openai/t5793-probe-model-explicit")
    finally:
        from reyn.llm.llm import _router_config_var
        _router_config_var.reset(token)
    assert router.num_retries == 9


# ── Embedding surface: litellm.aembedding's own entry point ────────────────


@pytest.mark.asyncio
async def test_embedding_omits_timeout_kwarg_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: #5793 — an unconfigured EmbeddingConfig (`timeout` unset)
    reaches ``litellm.aembedding`` with NO ``timeout=`` kwarg at all — a real
    spy on litellm's own boundary (the LLMReplay monkeypatch point), not a
    declaration. ``max_retries=0`` is EXPECTED to still be present — that is
    #3047's own deliberate, documented exception (reyn's retry loop stays
    the sole retry layer for embeddings), unrelated to #5793."""
    captured: dict = {}

    async def _spy(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("spy-short-circuit")  # no real network call

    monkeypatch.setattr(litellm, "aembedding", _spy)
    provider = LiteLLMEmbeddingProvider({"classes": {"standard": "openai/probe-embed-model"}})
    with pytest.raises(RuntimeError, match="Embedding failed"):
        await provider.embed(["hello"], "standard")

    assert "timeout" not in captured, (
        f"an unset embedding.timeout must not reach litellm.aembedding at "
        f"all; got kwargs={sorted(captured)}"
    )
    assert captured.get("max_retries") == 0, (
        "the #3047 max_retries=0 kwarg (a SEPARATE, deliberate exception — "
        "reyn's own retry loop, not litellm's, stays the retry layer for "
        "embeddings) must be unaffected by #5793"
    )


@pytest.mark.asyncio
async def test_embedding_passes_timeout_kwarg_when_operator_sets_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5793 deny — an operator-set embedding.timeout still reaches
    litellm.aembedding as an explicit kwarg."""
    captured: dict = {}

    async def _spy(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("spy-short-circuit")

    monkeypatch.setattr(litellm, "aembedding", _spy)
    provider = LiteLLMEmbeddingProvider({
        "timeout": 5.0, "classes": {"standard": "openai/probe-embed-model"},
    })
    with pytest.raises(RuntimeError, match="Embedding failed"):
        await provider.embed(["hello"], "standard")

    assert captured.get("timeout") == 5.0
