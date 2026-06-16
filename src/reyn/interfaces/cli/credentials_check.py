"""Credential pre-check for LLM-using commands.

Light/HN-user UX: surface "no API key set" as an actionable startup error
with explicit fix instructions, instead of a cryptic litellm
``InternalServerError: Missing credentials`` that fires after the
``reyn chat`` banner appears (and after ``reyn run`` has printed its
``skill / model / input`` header). The check covers the dominant
mis-configuration path — no env var AND no api_base proxy.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.interfaces.cli.session import Session


# Mapping from litellm provider prefix → required env var (litellm convention).
# Providers not listed here are not pre-checked; litellm raises its own
# error if their credentials are missing. This is intentionally narrow:
# false positives (= rejecting a setup that would actually work) are worse
# than false negatives (= late litellm error for an unusual provider).
_PROVIDER_ENV_VARS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "azure": "AZURE_API_KEY",
}


def verify_credentials_or_exit(
    session: "Session", args: argparse.Namespace,
) -> None:
    """Exit 1 with an actionable message if the model the command will use
    has no credentials configured (no env var AND no api_base proxy).
    """
    config = session.config

    # When a proxy api_base is configured the proxy handles auth — provider
    # env vars become optional (the proxy may accept any string or use its
    # own credential).  Skip the check entirely in that case.
    if config.api_base:
        return

    # Resolve to the litellm model string the command will actually use
    # (honours --model on the CLI).  Its provider prefix (everything before
    # the first "/") tells us which env var is required.
    try:
        _, resolved = session.model_for(args)
    except Exception:
        return  # resolver failure — let downstream surface its own error

    if "/" not in resolved:
        return  # bare model name; trust the resolver

    provider = resolved.split("/", 1)[0]
    env_var = _PROVIDER_ENV_VARS.get(provider)
    if env_var is None:
        return  # unknown provider — let litellm raise

    if os.environ.get(env_var):  # falsy on both None and empty string
        return

    sys.stderr.write(
        f"Error: no API key found for the configured model ({resolved}).\n"
        f"\n"
        f"Set the {env_var} environment variable:\n"
        f"    export {env_var}=<your-key>\n"
        f"\n"
        f"Or point reyn.local.yaml at a local proxy:\n"
        f"    echo 'api_base: http://localhost:4000' > reyn.local.yaml\n"
        f"\n"
        f"See docs/guide/getting-started/01-installation.md for details.\n"
    )
    sys.exit(1)
