"""REST router — /api/topologies.

Wraps AgentRegistry topology management. All topology data is a thin
pass-through from the Topology dataclass (P7).

Routes:
    GET    /api/topologies           — list all topologies
    POST   /api/topologies           — create a new topology
    GET    /api/topologies/{name}    — show one topology + its edges
    DELETE /api/topologies/{name}    — remove a topology
    POST   /api/topologies/{name}/members/{agent}   — add a member
    DELETE /api/topologies/{name}/members/{agent}   — remove a member
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from reyn.interfaces.web.deps import get_registry

router = APIRouter(tags=["topologies"])


# ── response models ───────────────────────────────────────────────────────────


class TopologySummary(BaseModel):
    name: str
    kind: str
    members: list[str]
    leader: str | None
    created_at: str
    edges: list[list[str]]  # [[from, to], ...]


class CreateTopologyRequest(BaseModel):
    name: str
    kind: str           # "network" | "team" | "pipeline"
    members: list[str]
    leader: str | None = None


# ── helpers ──────────────────────────────────────────────────────────────────


def _topo_to_summary(topo) -> TopologySummary:
    return TopologySummary(
        name=topo.name,
        kind=topo.kind,
        members=list(topo.members),
        leader=topo.leader,
        created_at=topo.created_at,
        edges=[[f, t] for f, t in topo.edges()],
    )


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/topologies", response_model=list[TopologySummary])
async def list_topologies(registry=Depends(get_registry)) -> list[TopologySummary]:
    """List all topologies including the auto-managed _default."""
    return [_topo_to_summary(t) for t in registry.list_topologies()]


@router.post("/topologies", response_model=TopologySummary, status_code=status.HTTP_201_CREATED)
async def create_topology(
    body: CreateTopologyRequest,
    registry=Depends(get_registry),
) -> TopologySummary:
    """Create a new named topology."""
    from reyn.runtime.topology import KINDS, Topology
    if body.kind not in KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid kind {body.kind!r}. Expected one of {KINDS}.",
        )
    try:
        topo = Topology.new(
            body.name,
            kind=body.kind,
            members=body.members,
            leader=body.leader,
        )
        registry.add_topology(topo)
    except FileExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Topology {body.name!r} already exists.",
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    return _topo_to_summary(topo)


@router.get("/topologies/{name}", response_model=TopologySummary)
async def get_topology(name: str, registry=Depends(get_registry)) -> TopologySummary:
    """Return one topology with its edge list."""
    try:
        topo = registry.get_topology(name)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {name!r} not found.",
        )
    return _topo_to_summary(topo)


@router.delete("/topologies/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topology(name: str, registry=Depends(get_registry)) -> None:
    """Remove a user-declared topology."""
    try:
        registry.remove_topology(name)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Topology {name!r} not found.",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


@router.post(
    "/topologies/{name}/members/{agent}",
    response_model=TopologySummary,
    status_code=status.HTTP_200_OK,
)
async def add_member(
    name: str,
    agent: str,
    registry=Depends(get_registry),
) -> TopologySummary:
    """Add an agent to an existing topology."""
    try:
        topo = registry.add_member(name, agent)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    return _topo_to_summary(topo)


@router.delete(
    "/topologies/{name}/members/{agent}",
    response_model=TopologySummary,
    status_code=status.HTTP_200_OK,
)
async def remove_member(
    name: str,
    agent: str,
    registry=Depends(get_registry),
) -> TopologySummary:
    """Remove an agent from a topology."""
    try:
        topo = registry.remove_member(name, agent)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    return _topo_to_summary(topo)
