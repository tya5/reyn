"""Argparse helpers shared by `reyn run`, `reyn eval`, and `reyn chat`.

All three subcommands accept the same set of flags: `--model`,
`--output-language`, and the runtime-limits flags (`--llm-timeout`,
`--llm-max-retries`). Each defaults to the corresponding
`safety.*` value from reyn.yaml (safety.loop.* / safety.timeout.*) and is
resolved by Session.limits_for().
"""
from __future__ import annotations

import argparse


def add_model_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model", default=None, metavar="MODEL",
        help=(
            "Model class name (light/standard/strong) or LiteLLM model string. "
            "Resolved via reyn.yaml models map. "
            "Default: from reyn.yaml 'model' key, or 'standard'."
        ),
    )


def add_output_language_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-language", default=None, dest="output_language", metavar="LANG",
        help="Output language code (default: from reyn.yaml or ja)",
    )


def add_limits_args(parser: argparse.ArgumentParser) -> None:
    """Add the runtime-limits flags (LLM timeout / retries)."""
    parser.add_argument(
        "--llm-timeout", dest="llm_timeout", type=float,
        default=None, metavar="SECONDS",
        help=(
            "Per-call LLM HTTP timeout (seconds). "
            "Default: from reyn.yaml `safety.timeout.llm_call_seconds`, "
            "or unset (litellm's own default) if that is also unset (#5793)."
        ),
    )
    parser.add_argument(
        "--llm-max-retries", dest="llm_max_retries", type=int,
        default=None, metavar="N",
        help=(
            "Transient-error retries per LLM call (litellm's own retry, not reyn's). "
            "Default: from reyn.yaml `safety.timeout.llm_max_retries`, "
            "or unset (litellm's own default) if that is also unset (#5793)."
        ),
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add --model, --output-language, and the runtime-limits flags to a subparser."""
    add_model_arg(parser)
    add_output_language_arg(parser)
    add_limits_args(parser)
