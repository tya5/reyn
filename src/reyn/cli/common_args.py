"""Argparse helpers shared by `reyn run`, `reyn eval`, and `reyn chat`.

All three subcommands accept the same set of flags: `--model`,
`--output-language`, and the runtime-limits flags (`--max-phase-visits`,
`--phase-budget`, `--llm-timeout`, `--llm-max-retries`). Each defaults to
the corresponding `limits.*` value from reyn.yaml and is resolved by
Session.limits_for().
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
    """Add the runtime-limits flags (visit cap, wall-clock budget, LLM timeout/retries)."""
    parser.add_argument(
        "--max-phase-visits", dest="max_phase_visits", type=int,
        default=None, metavar="N",
        help=(
            "Maximum times any single phase may be visited per run (0 = unlimited). "
            "Prevents infinite rollback/revision loops. "
            "Default: from reyn.yaml `limits.phase.max_visits` or 25."
        ),
    )
    parser.add_argument(
        "--phase-budget", dest="phase_budget", type=float,
        default=None, metavar="SECONDS",
        help=(
            "Per-phase wall-clock budget in seconds (0 = unlimited). "
            "Soft check at retry/turn boundaries — does not cancel mid-call. "
            "Default: from reyn.yaml `limits.phase.max_wall_seconds` or 0."
        ),
    )
    parser.add_argument(
        "--llm-timeout", dest="llm_timeout", type=float,
        default=None, metavar="SECONDS",
        help=(
            "Per-call LLM HTTP timeout (seconds). "
            "Default: from reyn.yaml `limits.llm.timeout` or 60."
        ),
    )
    parser.add_argument(
        "--llm-max-retries", dest="llm_max_retries", type=int,
        default=None, metavar="N",
        help=(
            "Transient-error retries per LLM call (LiteLLM exponential backoff). "
            "Default: from reyn.yaml `limits.llm.max_retries` or 3."
        ),
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add --model, --output-language, and the runtime-limits flags to a subparser."""
    add_model_arg(parser)
    add_output_language_arg(parser)
    add_limits_args(parser)
