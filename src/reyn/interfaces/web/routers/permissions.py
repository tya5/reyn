"""REST router — /api/permissions.

Wraps .reyn/approvals.yaml (per-process permission approval store).
All approval keys and values are passed through as-is (P7): the gateway
never interprets the semantics of approval keys (which encode skill-name
and path scoping from the engine's permission system).

Routes:
    GET    /api/permissions           — list all saved approvals
    DELETE /api/permissions/{key}     — revoke a single approval entry
    DELETE /api/permissions           — clear all approvals (body: {confirm: true})
"""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel

from reyn.interfaces.web.deps import get_project_root

router = APIRouter(tags=["permissions"])


# ── helpers ──────────────────────────────────────────────────────────────────


def _approvals_path(project_root: Path) -> Path:
    return project_root / ".reyn" / "approvals.yaml"


def _load(project_root: Path) -> dict[str, bool]:
    path = _approvals_path(project_root)
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): bool(v) for k, v in data.items() if isinstance(v, bool)}


def _save(data: dict[str, bool], project_root: Path) -> None:
    path = _approvals_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        path.write_text("{}\n", encoding="utf-8")
        return
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


# ── response models ───────────────────────────────────────────────────────────


class ApprovalEntry(BaseModel):
    key: str
    approved: bool


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/permissions", response_model=list[ApprovalEntry])
async def list_permissions(
    project_root: Path = Depends(get_project_root),
) -> list[ApprovalEntry]:
    """Return all saved approval entries from .reyn/approvals.yaml."""
    data = _load(project_root)
    return [ApprovalEntry(key=k, approved=v) for k, v in sorted(data.items())]


@router.delete("/permissions/{key:path}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_permission(
    key: str,
    project_root: Path = Depends(get_project_root),
) -> None:
    """Revoke a single approval entry by its key."""
    data = _load(project_root)
    if key not in data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No saved approval with key {key!r}.",
        )
    del data[key]
    _save(data, project_root)


@router.delete("/permissions", status_code=status.HTTP_204_NO_CONTENT)
async def clear_permissions(
    confirm: bool = Body(default=False, embed=True),
    project_root: Path = Depends(get_project_root),
) -> None:
    """Clear all saved approvals. Requires body: {\"confirm\": true}."""
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Pass {\"confirm\": true} in the request body to clear all approvals.",
        )
    _save({}, project_root)
