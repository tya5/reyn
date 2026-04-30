"""Argparse helpers shared by `reyn run`, `reyn eval`, and `reyn chat`.

These three subcommands all accept the same `--model`, `--output-language`,
and `--max-phase-visits` flags (each defaulting to the value resolved by
Session). Defining them in one place keeps the dest names, defaults, and
help text consistent.
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


def add_max_phase_visits_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-phase-visits", dest="max_phase_visits", type=int,
        default=None, metavar="N",
        help=(
            "Maximum times any single phase may be visited per run (0 = unlimited). "
            "Prevents infinite rollback/revision loops. Default: from reyn.yaml or 25."
        ),
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add --model, --output-language, --max-phase-visits to a subparser."""
    add_model_arg(parser)
    add_output_language_arg(parser)
    add_max_phase_visits_arg(parser)
