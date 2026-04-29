"""
Session: per-invocation bootstrap.

Loads config, applies environment, builds the model resolver. Each command
receives a Session so it can read effective values without re-running the
load/merge logic.
"""
from __future__ import annotations
import argparse
import os
from dataclasses import dataclass

from reyn.config import ReynConfig, load_config
from reyn.model_resolver import ModelResolver


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
        return m, self.resolver.resolve(m)

    def output_language_for(self, args: argparse.Namespace) -> str:
        return getattr(args, "output_language", None) or self.config.output_language

    def max_phase_visits_for(self, args: argparse.Namespace) -> int:
        v = getattr(args, "max_phase_visits", None)
        return v if v is not None else self.config.max_phase_visits

    def shell_allowed_for(self, args: argparse.Namespace) -> bool:
        return bool(getattr(args, "allow_shell", False)) or self.config.shell_allowed
