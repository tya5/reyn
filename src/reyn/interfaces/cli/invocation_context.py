"""
InvocationContext: per-invocation bootstrap.

Loads config, applies environment, builds the model resolver. Each command
receives an InvocationContext so it can read effective values without re-running the
load/merge logic.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from reyn.config import ReynConfig, SafetyConfig, load_config
from reyn.llm.model_resolver import ModelResolver


@dataclass
class InvocationContext:
    config: ReynConfig
    resolver: ModelResolver

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "InvocationContext":
        config = load_config()
        if config.api_base:
            os.environ.setdefault("LITELLM_API_BASE", config.api_base)
        return cls(config=config, resolver=ModelResolver(
            config.models,
            default_class=config.model,
            purpose_classes=config.model_class_by_purpose,
        ))

    # ── argparse-aware setting resolution (CLI > config) ─────────────────────

    def model_for(self, args: argparse.Namespace) -> tuple[str, str]:
        """Return (model_class_or_string, resolved_litellm_string)."""
        m = getattr(args, "model", None) or self.config.model
        return m, self.resolver.resolve(m).model

    def output_language_for(self, args: argparse.Namespace) -> str | None:
        """Resolve output_language with CLI > config priority.

        Returns None when neither CLI flag nor config provides a value —
        callers that need a concrete string for skill / phase paths
        should fall back to a domain-appropriate default (typically
        "ja"); the chat router uses None to skip the language directive
        in its system prompt entirely (= LLM picks based on user input).
        """
        cli = getattr(args, "output_language", None)
        if isinstance(cli, str) and cli.strip():
            return cli.strip()
        return self.config.output_language

    def safety_for(self, args: argparse.Namespace) -> SafetyConfig:
        """Resolve effective SafetyConfig with CLI flags layered over config.

        CLI flags (--max-phase-visits, --phase-budget, --llm-timeout,
        --llm-max-retries) override the corresponding safety.loop / safety.timeout
        fields while preserving everything else from the loaded config.
        """
        from reyn.config import LoopConfig, TimeoutConfig
        base = self.config.safety
        max_visits = getattr(args, "max_phase_visits", None)
        phase_budget = getattr(args, "phase_budget", None)
        llm_timeout = getattr(args, "llm_timeout", None)
        llm_max_retries = getattr(args, "llm_max_retries", None)

        # Only rebuild if at least one CLI override was provided.
        if any(v is not None for v in [max_visits, phase_budget, llm_timeout, llm_max_retries]):
            from dataclasses import replace
            loop = replace(
                base.loop,
                max_phase_visits=max_visits if max_visits is not None else base.loop.max_phase_visits,
            )
            timeout = replace(
                base.timeout,
                llm_call_seconds=llm_timeout if llm_timeout is not None else base.timeout.llm_call_seconds,
                llm_max_retries=llm_max_retries if llm_max_retries is not None else base.timeout.llm_max_retries,
                phase_seconds=phase_budget if phase_budget is not None else base.timeout.phase_seconds,
            )
            return SafetyConfig(loop=loop, timeout=timeout, on_limit=base.on_limit)
        return base

    # Keep limits_for as an alias that returns the safety config for backward
    # compat within this module (callers that were already updated use safety_for).
    def limits_for(self, args: argparse.Namespace) -> SafetyConfig:
        return self.safety_for(args)

