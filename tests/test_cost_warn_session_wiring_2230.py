"""Tier 2: the cost-warn config is threaded into the session (#2230).

The high-cost-model warn (#1830) and block (#1867) read it off the session. It was
never wired into the production-built session, so the read AttributeError'd into a
silent fail-open: a configured ``block_on_high_cost`` let a high-cost switch
through with no confirm. The pre-#2230 tests masked this by fabricating the config
on a fake session. These use a REAL Session with the config threaded via its
public ``cost_warn_config`` param, so removing the wiring (the param / the read)
turns the block test RED — the production bug reproduced.
"""
from __future__ import annotations

import pytest

from reyn.config import CostWarnConfig
from reyn.core.events.state_log import StateLog
from reyn.llm.model_resolver import ModelResolver
from reyn.runtime.model_cost_warn import maybe_block_high_cost_model
from reyn.runtime.session import Session


def _session(tmp_path, *, cost_warn: CostWarnConfig | None):
    # "expensive" resolves to azure/gpt-4 (~$30/1M input → above the $5 threshold).
    return Session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "wal.jsonl"),
        snapshot_path=tmp_path / "snap.json",
        resolver=ModelResolver({"expensive": "azure/gpt-4"}),
        cost_warn_config=cost_warn,
    )


@pytest.mark.asyncio
async def test_high_cost_block_fires_when_cost_warn_is_threaded(tmp_path) -> None:
    """Tier 2: with block_on_high_cost threaded, a high-cost switch is blocked.

    A non-interactive session fails closed (no confirm to show) — so a True
    ``block_on_high_cost`` denies. Before #2230 the session had no threaded
    config, the read fail-opened, and this returned True (allowed)."""
    session = _session(
        tmp_path,
        cost_warn=CostWarnConfig(
            enabled=True, block_on_high_cost=True,
            model_threshold_per_1m_input_usd=5.0,
        ),
    )
    session._non_interactive = True  # fail-closed path (no interactive checkpoint)
    allowed = await maybe_block_high_cost_model(
        session, "expensive", action="model_override"
    )
    assert allowed is False  # BLOCKED (the gate fired)


@pytest.mark.asyncio
async def test_default_cost_warn_does_not_block(tmp_path) -> None:
    """Tier 2: with no cost-warn config (the safe default, block off), a switch is
    allowed — the warn-only / head-less equivalence."""
    session = _session(tmp_path, cost_warn=None)
    allowed = await maybe_block_high_cost_model(
        session, "expensive", action="model_override"
    )
    assert allowed is True  # not blocked (default is warn-only)
