"""
InvocationContext: per-invocation bootstrap.

Loads config, applies environment, builds the model resolver. Each command
receives an InvocationContext so it can read effective values without re-running the
load/merge logic.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

from reyn.config import ReynConfig, SafetyConfig, load_config
from reyn.llm.model_resolver import ModelResolver


@dataclass
class InvocationContext:
    config: ReynConfig
    resolver: ModelResolver

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "InvocationContext":
        # #2683: ``LITELLM_API_BASE`` export folded into ``load_config()`` — the
        # single canonical writer (universal chokepoint). The former inline copy
        # here was purely redundant (``load_config`` above already exported it via
        # idempotent ``setdefault``); a single-writer AST guard now enforces this.
        config = load_config()
        resolver = ModelResolver(
            config.llm.models,
            default_class=config.llm.model,
            purpose_classes=config.llm.model_class_by_purpose,
            model_max_class=config.llm.model_max_class,  # #4206 T1 (②bounding)
        )
        # #4689: same registration registry_bootstrap.py's own
        # ModelResolver construction does — see that call site's comment.
        from reyn.llm.model_budget import register_max_input_overrides

        register_max_input_overrides(resolver.max_input_token_overrides())
        return cls(config=config, resolver=resolver)

    # ── argparse-aware setting resolution (CLI > config) ─────────────────────

    def model_for(self, args: argparse.Namespace) -> tuple[str, str]:
        """Return (model_class_or_string, resolved_litellm_string)."""
        m = getattr(args, "model", None) or self.config.llm.model
        return m, self.resolver.resolve(m).model

    def output_language_for(self, args: argparse.Namespace) -> str | None:
        """Resolve output_language with CLI > config priority.

        Returns None when neither CLI flag nor config provides a value —
        callers that need a concrete string for phase paths
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

        CLI flags (--llm-timeout, --llm-max-retries) override the corresponding
        safety.timeout fields while preserving everything else from the loaded
        config.
        """
        base = self.config.safety
        llm_timeout = getattr(args, "llm_timeout", None)
        llm_max_retries = getattr(args, "llm_max_retries", None)

        # Only rebuild if at least one CLI override was provided.
        if any(v is not None for v in [llm_timeout, llm_max_retries]):
            from dataclasses import replace
            timeout = replace(
                base.timeout,
                llm_call_seconds=llm_timeout if llm_timeout is not None else base.timeout.llm_call_seconds,
                llm_max_retries=llm_max_retries if llm_max_retries is not None else base.timeout.llm_max_retries,
            )
            return SafetyConfig(loop=base.loop, timeout=timeout, on_limit=base.on_limit)
        return base

    # Keep limits_for as an alias that returns the safety config for backward
    # compat within this module (callers that were already updated use safety_for).
    def limits_for(self, args: argparse.Namespace) -> SafetyConfig:
        return self.safety_for(args)

