"""
Session: per-invocation bootstrap.

Loads config, applies environment, builds the model resolver. Each command
receives a Session so it can read effective values without re-running the
load/merge logic.
"""
from __future__ import annotations
import argparse
import os
from dataclasses import dataclass, replace

from reyn.config import LimitsConfig, LLMLimitsConfig, PhaseLimitsConfig, ReynConfig, load_config
from reyn.llm.model_resolver import ModelResolver


@dataclass
class Session:
    config: ReynConfig
    resolver: ModelResolver

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Session":
        config = load_config()
        if config.api_base:
            os.environ.setdefault("LITELLM_API_BASE", config.api_base)
        return cls(config=config, resolver=ModelResolver(config.models))

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

    def limits_for(self, args: argparse.Namespace) -> LimitsConfig:
        """Resolve effective LimitsConfig with CLI flags layered over config."""
        base = self.config.limits
        max_visits = getattr(args, "max_phase_visits", None)
        phase_budget = getattr(args, "phase_budget", None)
        llm_timeout = getattr(args, "llm_timeout", None)
        llm_max_retries = getattr(args, "llm_max_retries", None)
        return LimitsConfig(
            llm=LLMLimitsConfig(
                timeout=llm_timeout if llm_timeout is not None else base.llm.timeout,
                max_retries=llm_max_retries if llm_max_retries is not None else base.llm.max_retries,
            ),
            phase=PhaseLimitsConfig(
                max_visits=max_visits if max_visits is not None else base.phase.max_visits,
                max_wall_seconds=phase_budget if phase_budget is not None else base.phase.max_wall_seconds,
            ),
        )

    def shell_allowed_for(self, args: argparse.Namespace) -> bool:
        return bool(getattr(args, "allow_shell", False)) or self.config.shell_allowed
