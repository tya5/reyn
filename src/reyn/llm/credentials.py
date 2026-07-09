"""Typed LLM credential pre-check for the single ``recorded_acompletion`` funnel.

Light/HN-user UX: surface "no API key set" as an actionable, typed error the
moment a run is *about to* make its first LLM call, instead of a cryptic litellm
``InternalServerError: Missing credentials`` leaking out of the provider layer.

This lives in ``reyn.llm`` (not ``interfaces.cli``) on purpose: the check now
rides the **LLM-call axis** — ``recorded_acompletion`` is the one place ALL LLM
calls funnel through (#1190 AST-guarded) — rather than the un-enumerable
surface axis. Every surface (CLI / web / chainlit / dogfood / agent-step spawn /
pipeline driver) reaches the funnel, so the friendly missing-cred error is
universal *by construction*: no per-surface startup gate to hand-wire, forget,
or let drift (the #2708 divergence class). Each surface's existing error
boundary renders :class:`MissingCredentialsError` like any other typed error
(CLI → friendly stderr + exit; web/A2A → 4xx; chainlit → error bubble; mcp →
error result).

The check is deliberately **narrow**: false positives (rejecting a setup that
would actually work) are worse than false negatives (a late litellm error for
an unusual provider). Only "known-provider prefix + that provider's env var
unset + no proxy ``api_base``" is a miss; every other input passes through so
the resolver / litellm own their errors. Because the check fires INSIDE the
funnel, an LLM-less run (a transform/tool-only ``pipe run``) never reaches it
and thus can never be rejected for missing creds — the #2686 false-positive-zero
property is now structural, not a hand-maintained guard.
"""
from __future__ import annotations

import os

# Mapping from litellm provider prefix → required env var (litellm convention).
# Providers not listed here are not pre-checked; litellm raises its own error if
# their credentials are missing. Intentionally narrow (see module docstring).
_PROVIDER_ENV_VARS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "azure": "AZURE_API_KEY",
}


class MissingCredentialsError(Exception):
    """The configured model needs a provider env var that is unset (and there is
    no proxy ``api_base`` to handle auth). Raised at the ``recorded_acompletion``
    funnel BEFORE any provider call, so surfaces render an actionable message
    instead of a raw litellm ``Missing credentials`` stack.

    Carries ``model`` / ``provider`` / ``env_var`` so a surface can render its
    own affordance; :meth:`user_message` is the shared human-readable body.
    """

    def __init__(self, *, model: str, provider: str, env_var: str) -> None:
        self.model = model
        self.provider = provider
        self.env_var = env_var
        super().__init__(self.user_message())

    def user_message(self) -> str:
        """Actionable body (no leading ``Error:`` — the surface prefixes as it
        sees fit) naming the exact env var to set and the proxy alternative."""
        return (
            f"no API key found for the configured model ({self.model}).\n"
            f"\n"
            f"Set the {self.env_var} environment variable:\n"
            f"    export {self.env_var}=<your-key>\n"
            f"\n"
            f"Or point reyn.local.yaml at a local proxy:\n"
            f"    echo 'api_base: http://localhost:4000' > reyn.local.yaml\n"
            f"\n"
            f"See docs/guide/getting-started/01-installation.md for details."
        )


def check_model_credentials(
    *, model: str | None, api_base: str | None,
) -> "MissingCredentialsError | None":
    """Pure verdict: return a :class:`MissingCredentialsError` iff the resolved
    litellm ``model`` string needs a provider env var that is unset AND no proxy
    ``api_base`` is in effect. Otherwise return ``None`` (no side effects, no
    ``sys.exit`` — the caller decides how to surface the verdict).

    The ONLY miss case is "no ``api_base`` + known provider prefix + that
    provider's env var unset". Every other input passes (``api_base`` set → the
    proxy handles auth; ``model`` None/bare/unknown-provider → trust the
    resolver / let litellm raise its own error). ``api_base`` is the EFFECTIVE
    proxy signal at the funnel (per-class routing OR the global proxy), a
    superset of the old config-level ``config.api_base`` check.
    """
    # A proxy api_base handles auth — provider env vars become optional. Skip.
    if api_base:
        return None
    # None = the caller could not resolve a model string — let downstream raise.
    if model is None:
        return None
    if "/" not in model:
        return None  # bare model name; trust the resolver
    provider = model.split("/", 1)[0]
    env_var = _PROVIDER_ENV_VARS.get(provider)
    if env_var is None:
        return None  # unknown provider — let litellm raise
    if os.environ.get(env_var):  # falsy on both None and empty string
        return None
    return MissingCredentialsError(model=model, provider=provider, env_var=env_var)
