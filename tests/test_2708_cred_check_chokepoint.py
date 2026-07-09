"""Tier 2: #2708 P3.2b — the ONE missing-cred pre-check at the LLM funnel.

The missing-cred check moved OFF three per-surface startup gates (chat / mcp /
pipe) and ONTO the single LLM funnel ``recorded_acompletion`` — the one place ALL
LLM calls funnel through (#1190). This file pins the resulting OS invariants:

- ``check_model_credentials`` is a PURE verdict (returns ``MissingCredentialsError
  | None``, no ``sys.exit``), preserving the deliberately-narrow contract EXACTLY:
  only "known provider prefix + that env var unset + no proxy ``api_base``" is a
  miss; proxy / env-set / None / bare / unknown-provider all pass (false positives
  are worse than false negatives).
- ``recorded_acompletion`` RAISES ``MissingCredentialsError`` BEFORE it touches
  litellm when the model needs an unset key and no proxy is in effect. Because
  every surface (CLI / web / chainlit / dogfood / agent-step spawn / pipeline
  driver) funnels here, this makes the friendly missing-cred error universal by
  construction — the property this PR delivers. RED on main (no funnel check
  exists there; web/chainlit/dogfood skip the check entirely today).
- The CLI error boundary (``reyn.interfaces.cli.main``) renders the typed error
  as the same actionable "no API key" message + exit 1 the removed startup gate
  printed.

Real ``recorded_acompletion`` / real ``ArgumentParser`` / real ``main`` — no
mocks. The raising path needs no network (the check fires before any provider
call), so no ``LLMReplay`` fixture is required.
"""
from __future__ import annotations

import argparse
import os
import sys

import pytest

from reyn.llm.credentials import MissingCredentialsError, check_model_credentials
from reyn.llm.llm import recorded_acompletion

_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AZURE_API_KEY")


@pytest.fixture(autouse=True)
def _restore_litellm_api_base():
    """Snapshot + restore LITELLM_API_BASE around each test (mirrors the guard in
    ``test_2686_pipe_llm_credential_precheck.py``): the proxy signal is process-
    wide, so a leak would perturb litellm-routing tests elsewhere."""
    saved = os.environ.get("LITELLM_API_BASE")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("LITELLM_API_BASE", None)
        else:
            os.environ["LITELLM_API_BASE"] = saved


@pytest.fixture
def _keys_unset(monkeypatch, _provider_credentials_present):
    """Every provider env var + the proxy switch unset — a genuinely-
    uncredentialled run. Depends on ``_provider_credentials_present`` (the
    conftest autouse dummy-cred fixture) so this ``delenv`` runs AFTER its
    ``setenv`` and wins."""
    for key in _PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    os.environ.pop("LITELLM_API_BASE", None)


# ---------------------------------------------------------------------------
# check_model_credentials — the pure narrow verdict
# ---------------------------------------------------------------------------


def test_check_returns_error_when_key_unset_and_no_proxy(_keys_unset) -> None:
    """Tier 2: no api_base + known provider prefix + env var unset → a
    ``MissingCredentialsError`` verdict carrying model/provider/env_var. This is
    the ONLY miss case."""
    verdict = check_model_credentials(model="openai/gpt-4o-mini", api_base=None)
    assert isinstance(verdict, MissingCredentialsError)
    assert verdict.model == "openai/gpt-4o-mini"
    assert verdict.provider == "openai"
    assert verdict.env_var == "OPENAI_API_KEY"


def test_check_none_when_env_set(_keys_unset, monkeypatch) -> None:
    """Tier 2: provider env var set → no miss (verdict None)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert check_model_credentials(model="openai/gpt-4o-mini", api_base=None) is None


def test_check_none_under_proxy_api_base(_keys_unset) -> None:
    """Tier 2: a proxy api_base in effect → no miss even with the key unset (the
    proxy handles auth). This is the effective-proxy signal at the funnel — a
    superset of the old config-level ``config.api_base`` check (per-class routing
    OR the global proxy both count)."""
    assert check_model_credentials(
        model="openai/gpt-4o-mini", api_base="http://localhost:4000",
    ) is None


def test_check_none_for_none_bare_and_unknown_provider(_keys_unset) -> None:
    """Tier 2: resolver-failure (None), a bare model name (no '/'), and an
    unknown provider prefix all pass — the check stays deliberately narrow
    (false positives are worse than a late litellm error)."""
    assert check_model_credentials(model=None, api_base=None) is None
    assert check_model_credentials(model="gpt-4o-mini", api_base=None) is None
    assert check_model_credentials(model="cohere/command-r", api_base=None) is None


def test_missing_credentials_error_message_is_actionable(_keys_unset) -> None:
    """Tier 2: the error body names the exact env var to export and the proxy
    alternative — the actionable content every surface renders."""
    verdict = check_model_credentials(model="anthropic/claude-3-5", api_base=None)
    assert isinstance(verdict, MissingCredentialsError)
    msg = verdict.user_message().lower()
    assert "no api key" in msg
    assert "anthropic_api_key" in msg
    assert "api_base" in msg  # the proxy alternative is offered


# ---------------------------------------------------------------------------
# recorded_acompletion — the universal funnel raises before litellm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_funnel_raises_missing_credentials_before_litellm(_keys_unset) -> None:
    """Tier 2: the single LLM funnel raises ``MissingCredentialsError`` when the
    model needs an unset key and no proxy is configured — BEFORE any provider
    call (no network). This is the universal-by-construction property: every
    surface funnels through ``recorded_acompletion``, so CLI/web/chainlit/dogfood
    and every agent-step / pipeline-driver spawn get the friendly error for free.
    RED on main (the funnel has no cred check there)."""
    with pytest.raises(MissingCredentialsError) as exc:
        await recorded_acompletion(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            purpose="main",
        )
    assert exc.value.env_var == "OPENAI_API_KEY"


@pytest.mark.asyncio
async def test_funnel_does_not_raise_missing_creds_under_routing_proxy(_keys_unset) -> None:
    """Tier 2: when per-class routing supplies a proxy ``api_base``, the funnel
    does NOT raise ``MissingCredentialsError`` even with the key unset (the proxy
    handles auth). The call then proceeds to the provider layer and fails there
    for an unrelated reason (unreachable test endpoint) — never a cred rejection.
    Pins that the check reads the EFFECTIVE proxy signal, not just config."""
    with pytest.raises(Exception) as exc:  # noqa: PT011 — asserting the TYPE below
        await recorded_acompletion(
            model="openai/gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            purpose="main",
            routing={
                "api_base": "http://127.0.0.1:9",  # unreachable — provider-layer fail
                "custom_llm_provider": "openai",
                "api_key": "dummy",
                "num_retries": 0,
            },
        )
    assert not isinstance(exc.value, MissingCredentialsError)


# ---------------------------------------------------------------------------
# CLI error boundary renders the typed error
# ---------------------------------------------------------------------------


def test_cli_main_renders_missing_credentials_and_exits(monkeypatch, capsys) -> None:
    """Tier 2: the CLI error boundary (``main``) catches the typed error from the
    funnel and renders the same actionable "no API key" message + exit 1 the
    removed per-surface startup gate printed. Real ``main`` + real
    ``ArgumentParser`` (the parser factory is stubbed only to inject a command
    whose run raises — no collaborator is mocked)."""
    import reyn.interfaces.cli as cli

    def _boom(_args) -> None:
        raise MissingCredentialsError(
            model="openai/gpt-4o-mini", provider="openai", env_var="OPENAI_API_KEY",
        )

    def _fake_parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        parser.set_defaults(func=_boom)
        return parser

    monkeypatch.setattr(cli, "build_parser", _fake_parser)
    monkeypatch.setattr(sys, "argv", ["reyn"])

    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err.lower()
    assert "no api key" in err
    assert "openai_api_key" in err
