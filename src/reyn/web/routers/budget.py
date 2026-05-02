"""REST router — /api/budget.

Surfaces BudgetTracker usage counters and configuration caps.

Note: PATCH /api/budget/caps modifies the in-process config only (not
persisted to reyn.yaml). Persisting cap changes requires editing reyn.yaml
directly — this is by design, as the budget config is authoritative in
the config file.

Routes:
    GET   /api/budget/usage   — current in-process usage counters
    PATCH /api/budget/caps    — update hard caps on the live tracker (in-process only)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from reyn.web.deps import get_budget_tracker, get_reyn_config

router = APIRouter(tags=["budget"])


# ── response models ───────────────────────────────────────────────────────────


class BudgetCapDetail(BaseModel):
    hard_limit: float | None
    warn_ratio: float


class BudgetCaps(BaseModel):
    per_agent_tokens: BudgetCapDetail
    per_agent_cost_usd: BudgetCapDetail
    per_chain_skill_calls: BudgetCapDetail
    per_chain_skill_tokens: BudgetCapDetail
    daily_tokens: BudgetCapDetail
    daily_cost_usd: BudgetCapDetail
    monthly_tokens: BudgetCapDetail
    monthly_cost_usd: BudgetCapDetail


class BudgetUsage(BaseModel):
    daily_tokens: int
    daily_cost_usd: float
    monthly_tokens: int
    monthly_cost_usd: float
    # Per-agent totals (in-process accumulation only, resets on restart)
    per_agent_tokens: dict[str, int]
    per_agent_cost_usd: dict[str, float]
    caps: BudgetCaps


class PatchCapsRequest(BaseModel):
    daily_tokens_hard_limit: float | None = None
    daily_cost_usd_hard_limit: float | None = None
    monthly_tokens_hard_limit: float | None = None
    monthly_cost_usd_hard_limit: float | None = None
    per_agent_tokens_hard_limit: float | None = None
    per_agent_cost_usd_hard_limit: float | None = None


# ── helpers ──────────────────────────────────────────────────────────────────


def _cap_detail(cap_cfg) -> BudgetCapDetail:
    return BudgetCapDetail(
        hard_limit=cap_cfg.hard_limit,
        warn_ratio=cap_cfg.warn_ratio,
    )


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/budget/usage", response_model=BudgetUsage)
async def get_budget_usage(
    tracker=Depends(get_budget_tracker),
    config=Depends(get_reyn_config),
) -> BudgetUsage:
    """Return current in-process budget usage and configured caps."""
    cfg = tracker.config
    return BudgetUsage(
        daily_tokens=tracker._daily_tokens,
        daily_cost_usd=tracker._daily_cost_usd,
        monthly_tokens=tracker._monthly_tokens,
        monthly_cost_usd=tracker._monthly_cost_usd,
        per_agent_tokens=dict(tracker._agent_tokens),
        per_agent_cost_usd=dict(tracker._agent_cost_usd),
        caps=BudgetCaps(
            per_agent_tokens=_cap_detail(cfg.per_agent_tokens),
            per_agent_cost_usd=_cap_detail(cfg.per_agent_cost_usd),
            per_chain_skill_calls=_cap_detail(cfg.per_chain_skill_calls),
            per_chain_skill_tokens=_cap_detail(cfg.per_chain_skill_tokens),
            daily_tokens=_cap_detail(cfg.daily_tokens),
            daily_cost_usd=_cap_detail(cfg.daily_cost_usd),
            monthly_tokens=_cap_detail(cfg.monthly_tokens),
            monthly_cost_usd=_cap_detail(cfg.monthly_cost_usd),
        ),
    )


@router.patch("/budget/caps", response_model=BudgetUsage)
async def patch_budget_caps(
    body: PatchCapsRequest,
    tracker=Depends(get_budget_tracker),
    config=Depends(get_reyn_config),
) -> BudgetUsage:
    """Update hard caps on the live tracker (in-process only, not persisted).

    To make changes permanent, edit reyn.yaml directly.
    """
    from reyn.budget.budget import CostLimitConfig
    cfg = tracker.config

    def _apply(field_name: str, new_hard_limit: float | None) -> None:
        if new_hard_limit is None:
            return
        current: CostLimitConfig = getattr(cfg, field_name)
        updated = CostLimitConfig(
            hard_limit=new_hard_limit,
            warn_ratio=current.warn_ratio,
        )
        setattr(cfg, field_name, updated)

    _apply("daily_tokens", body.daily_tokens_hard_limit)
    _apply("daily_cost_usd", body.daily_cost_usd_hard_limit)
    _apply("monthly_tokens", body.monthly_tokens_hard_limit)
    _apply("monthly_cost_usd", body.monthly_cost_usd_hard_limit)
    _apply("per_agent_tokens", body.per_agent_tokens_hard_limit)
    _apply("per_agent_cost_usd", body.per_agent_cost_usd_hard_limit)

    # Return updated state
    return await get_budget_usage(tracker=tracker, config=config)
