"""REST router — /api/agents.

Wraps AgentRegistry and AgentProfile. The gateway treats agent data as
opaque structured values: it passes profile fields through without
interpreting skill-domain semantics (P7).

Routes:
    GET  /api/agents            — list all agents
    POST /api/agents            — create a new agent
    GET  /api/agents/{name}     — show one agent (profile + last activity)
    DELETE /api/agents/{name}   — remove an agent
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from reyn.interfaces.web.deps import get_registry

router = APIRouter(tags=["agents"])


# ── request / response models ────────────────────────────────────────────────


class AgentSummary(BaseModel):
    name: str
    role: str
    created_at: str
    allowed_skills: list[str] | None
    last_activity_at: str | None


class CreateAgentRequest(BaseModel):
    name: str
    role: str = ""
    allowed_skills: list[str] | None = None


class AgentDetail(AgentSummary):
    pass


# ── helpers ──────────────────────────────────────────────────────────────────


def _profile_to_summary(registry, name: str) -> AgentSummary:
    profile = registry.load_profile(name)
    last_at = registry.last_activity_at(name)
    return AgentSummary(
        name=profile.name,
        role=profile.role,
        created_at=profile.created_at,
        allowed_skills=profile.allowed_skills,
        last_activity_at=last_at.isoformat() if last_at else None,
    )


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/agents", response_model=list[AgentSummary])
async def list_agents(registry=Depends(get_registry)) -> list[AgentSummary]:
    """List all agents found on disk."""
    names = registry.list_names()
    return [_profile_to_summary(registry, n) for n in names]


@router.post("/agents", response_model=AgentDetail, status_code=status.HTTP_201_CREATED)
async def create_agent(
    body: CreateAgentRequest,
    registry=Depends(get_registry),
) -> AgentDetail:
    """Create a new agent with the given name and optional role."""
    try:
        profile = registry.create(body.name, role=body.role or "")
    except FileExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent {body.name!r} already exists.",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    # Save allowed_skills if provided.
    if body.allowed_skills is not None:
        from reyn.runtime.profile import AgentProfile
        updated = AgentProfile(
            name=profile.name,
            role=profile.role,
            created_at=profile.created_at,
            allowed_skills=body.allowed_skills,
        )
        agent_dir = registry._dir / profile.name
        updated.save(agent_dir)
        profile = updated

    return AgentDetail(
        name=profile.name,
        role=profile.role,
        created_at=profile.created_at,
        allowed_skills=profile.allowed_skills,
        last_activity_at=None,
    )


@router.get("/agents/{name}", response_model=AgentDetail)
async def get_agent(name: str, registry=Depends(get_registry)) -> AgentDetail:
    """Return profile and last-activity timestamp for a single agent."""
    if not registry.exists(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {name!r} not found.",
        )
    return AgentDetail(**_profile_to_summary(registry, name).model_dump())


@router.delete("/agents/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(name: str, registry=Depends(get_registry)) -> None:
    """Remove an agent and its on-disk state."""
    if not registry.exists(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent {name!r} not found.",
        )
    try:
        registry.remove(name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
