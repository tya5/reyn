"""Tier 2: OS invariant — Agent.from_config wiring factory (#997 dir2, PR-A).

``Agent.from_config`` is the construction-time prevention of the FP-0008 / #1133
wiring-gap class: it derives the permission/runtime bundle (permission_resolver,
mcp_servers, python_allowed_modules, prompt_cache_enabled, sandbox_config,
resolver) from a ReynConfig so a direct caller cannot omit it. ``model`` /
``safety`` / ``resolver`` / ``python_allowed_modules`` default to the
config-derived value but accept an override.

These pin the public, observable contract of the factory: the config-default and
override behavior of ``model``, and that construction succeeds for both
shell_allowed modes (the perm-resolver derivation — including the shell
pre-approval branch — runs without the caller supplying a resolver). The 3
migrated CLI callers (run / cron / mcp, this PR) exercise the full bundle through
their existing command tests; PR-B adds the AST omit-pin (Agent() only via
from_config) for the structural "cannot omit" guarantee, alongside the dir3
``phase_op_catalog_gap`` runtime event (#1152) that catches any residual gap.

No mocks — real ReynConfig + real Agent.
"""
from __future__ import annotations

from reyn.agent import Agent
from reyn.config import ReynConfig


def test_from_config_model_defaults_to_config() -> None:
    """Tier 2: from_config(config) uses config.model when no override is given."""
    cfg = ReynConfig(model="standard")
    agent = Agent.from_config(cfg, interactive=False)
    assert agent.model == "standard"


def test_from_config_model_override_wins() -> None:
    """Tier 2: an explicit model override replaces the config default."""
    cfg = ReynConfig(model="standard")
    agent = Agent.from_config(cfg, model="strong", interactive=False)
    assert agent.model == "strong"



def test_from_config_caller_override() -> None:
    """Tier 2: a non-default caller (validated by Agent.__init__) is forwarded."""
    cfg = ReynConfig(model="standard")
    agent = Agent.from_config(
        cfg, caller="agents/reviewer", interactive=False
    )
    assert agent.caller == "agents/reviewer"
