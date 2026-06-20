"""Tier 2: OS-invariant tests for #1829 S4 — credential rotation.

S4 adds ``llm.router.credentials`` (model → [{api_key_env: NAME}]): each USABLE key
becomes a Router deployment with the same model_name (the Router rotates / fails
over across keys). Decisions (lead-confirmed): keys are referenced by ENV-VAR NAME
only (read from os.environ at build); a missing env is skipped with a warning; a
DECLARED model whose keys ALL resolve to nothing raises (fail-loud, no silent
keyless deployment).

SECURITY (the impl gate): the key VALUE must never appear in the cache fingerprint
(env-var NAME only) — pinned here WITH falsification (a sentinel value that would
show up if the impl ever fingerprinted the resolved key). The #1669 redaction
backstop (api_key stripped from the llm_request event) is also pinned.

Policy: no mocks of Reyn collaborators — real RouterConfig + real builder +
real litellm.Router. litellm.acompletion is patched only because it IS the replay
boundary (used to observe which key each rotated call threads). Tier line first.
"""
from __future__ import annotations

from unittest import mock

import litellm
import pytest

import reyn.llm.llm as llm_mod
from reyn.config.infra import RouterConfig
from reyn.llm.llm import (
    _deployments_for_model,
    _redact_llm_request_params,
    _router_cache_fingerprint,
    _single_deployment_router,
    set_router_config,
)

_M = "openai/gpt-4o-mini"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REYN_LLM_USE_ROUTER", raising=False)
    llm_mod._router_config_var.set(None)
    before = dict(litellm.model_cost)
    yield
    llm_mod._router_config_var.set(None)
    litellm.model_cost.clear()
    litellm.model_cost.update(before)


def _cfg(**kw) -> RouterConfig:
    return RouterConfig(use=True, **kw)


# ── deployment expansion ─────────────────────────────────────────────────────

def test_credentials_expand_to_one_deployment_per_usable_key(monkeypatch) -> None:
    """Tier 2: each usable key → one deployment (same model_name) for rotation."""
    monkeypatch.setenv("K1", "val-1")
    monkeypatch.setenv("K2", "val-2")
    cfg = _cfg(credentials={_M: [{"api_key_env": "K1"}, {"api_key_env": "K2"}]})
    deps = _deployments_for_model(_M, cfg)
    assert all(d["model_name"] == _M for d in deps), "same model_name → Router rotates"
    # both keys present as distinct deployments (set equality ⇒ two distinct keys).
    assert {d["litellm_params"]["api_key"] for d in deps} == {"val-1", "val-2"}


def test_missing_env_key_skipped_remaining_used(monkeypatch) -> None:
    """Tier 2: a missing env var is skipped (degrade); the remaining key still builds."""
    monkeypatch.setenv("K1", "val-1")
    monkeypatch.delenv("K2", raising=False)
    cfg = _cfg(credentials={_M: [{"api_key_env": "K1"}, {"api_key_env": "K2"}]})
    deps = _deployments_for_model(_M, cfg)
    # exactly the one usable key survives (K2 skipped) — list equality pins it.
    assert [d["litellm_params"]["api_key"] for d in deps] == ["val-1"]


def test_all_credentials_unusable_raises(monkeypatch) -> None:
    """Tier 2: a DECLARED credentials[model] resolving to ZERO usable keys raises
    (fail-loud — never a silent keyless deployment, lead decision 2)."""
    monkeypatch.delenv("K1", raising=False)
    monkeypatch.delenv("K2", raising=False)
    cfg = _cfg(credentials={_M: [{"api_key_env": "K1"}, {"api_key_env": "K2"}]})
    with pytest.raises(RuntimeError):
        _deployments_for_model(_M, cfg)


def test_no_credentials_single_plain_deployment() -> None:
    """Tier 2: no credentials → one plain deployment with no api_key (S3b behavior)."""
    deps = _deployments_for_model(_M, _cfg())
    assert deps == [{"model_name": _M, "litellm_params": {"model": _M}}]


# ── SECURITY: key VALUE never fingerprinted (falsification) ───────────────────

def test_fingerprint_excludes_secret_value_includes_name(monkeypatch) -> None:
    """Tier 2: (security) the cache fingerprint contains the env-var NAME but NEVER
    the resolved key VALUE. Falsification: ``_SECRET_SENTINEL`` is the live value of
    the env var — if the impl ever fingerprinted the resolved key (instead of the
    name), this assertion goes red."""
    monkeypatch.setenv("KSEC", "_SECRET_SENTINEL_VALUE_")
    cfg = _cfg(credentials={_M: [{"api_key_env": "KSEC"}]})
    fp_repr = repr(_router_cache_fingerprint(cfg))
    assert "_SECRET_SENTINEL_VALUE_" not in fp_repr, "secret VALUE must NEVER be fingerprinted"
    assert "KSEC" in fp_repr, "the env-var NAME identifies the credential in the key"


def test_redact_strips_api_key_value() -> None:
    """Tier 2: (security backstop) the #1669 llm_request-event redaction masks an
    api_key value (double defense; the proxy path injects api_key)."""
    redacted = _redact_llm_request_params({"api_key": "_SECRET_KEY_VALUE_", "model": _M}, None)
    assert "_SECRET_KEY_VALUE_" not in repr(redacted), "api_key value must be redacted"


# ── rotation through the (real) Router → litellm.acompletion ──────────────────

def _deployment_api_key(d) -> str | None:
    """api_key from a get_model_list() entry (dict or object, across litellm versions)."""
    lp = d.get("litellm_params", {}) if isinstance(d, dict) else getattr(d, "litellm_params", {})
    return lp.get("api_key") if isinstance(lp, dict) else getattr(lp, "api_key", None)


@pytest.mark.asyncio
async def test_rotation_wires_both_keys_and_routes_through_litellm_acompletion(monkeypatch) -> None:
    """Tier 2: a 2-key credentials chain builds a Router that wires BOTH keys as
    deployments and routes through the (monkeypatched) litellm.acompletion boundary.

    Bounded-by-construction (#1888 de-flake): the asserted rotation invariant is the
    DETERMINISTIC one — both keys are present as Router deployments (read from
    get_model_list()). The runtime load-balance DISTRIBUTION across them is litellm's
    `simple-shuffle` (weighted-random), so the prior "both keys appear in 6 calls"
    assertion was probabilistic and flaked (~3% of 6-call runs land entirely on one
    deployment → got {'val-2'}, which blocked #1885). Wiring + the acompletion
    boundary are timing/probability-independent."""
    monkeypatch.setenv("K1", "val-1")
    monkeypatch.setenv("K2", "val-2")
    set_router_config(_cfg(credentials={_M: [{"api_key_env": "K1"}, {"api_key_env": "K2"}]}))
    seen: list = []

    async def _fake(*a, **k):
        seen.append(k.get("api_key"))

        class _R:
            choices = [object()]
            model = _M
            usage = None
        return _R()

    with mock.patch.object(litellm, "acompletion", side_effect=_fake):
        router = _single_deployment_router(_M)
        # deterministic: BOTH rotated keys are wired as Router deployments.
        wired = {_deployment_api_key(d) for d in (router.get_model_list() or [])}
        assert {"val-1", "val-2"} <= wired, (
            f"both rotated keys must be wired as Router deployments (got {wired!r})"
        )
        # behavioral: a call routes through the (monkeypatched) litellm.acompletion.
        await router.acompletion(model=_M, messages=[{"role": "user", "content": "x"}])
    assert seen, "the Router must route through the monkeypatched litellm.acompletion"
