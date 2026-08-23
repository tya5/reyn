"""REST router — /api/permissions.

Wraps the #5153 append-only ``approvals.jsonl`` ledger (per-project
permission-approval decision log — see
``reyn.security.permissions.approval_ledger``). All approval keys and
values are passed through as-is (P7): the gateway never interprets the
semantics of approval keys (which encode actor and path scoping from the
engine's permission system).

Routes:
    GET    /api/permissions           — list all saved approvals
    DELETE /api/permissions/{key}     — revoke a single approval entry
    DELETE /api/permissions           — clear all approvals (body: {confirm: true})
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel

from reyn.core.events.events import emit_direct_event
from reyn.interfaces.web.deps import get_project_root

router = APIRouter(tags=["permissions"])

# #5065: the audit-event surface label — both the ``emit_direct_event`` seam's
# own directory/emitter axis (``.reyn/events/direct/web/``) AND the payload's
# ``surface`` field (part of the new kinds' required fields below) are stamped
# from this ONE constant, so the two never drift apart.
_SURFACE = "web"


# ── helpers ──────────────────────────────────────────────────────────────────

# #5153 (architect ruling, issuecomment-5383838646, scope confirmed
# issuecomment-5383848849): this router used to read/write
# `.reyn/approvals.yaml` directly with its own `_load`/`_save`, a THIRD
# independent writer alongside `PermissionResolver._persist` and the CLI
# `permissions` command's own equivalent — all 3 racing the same snapshot
# read-modify-write. Moved onto the same `ApprovalLedger` every other
# approvals surface now uses.


def _ledger_path(project_root: Path) -> Path:
    # #5173: derived from the SAME constant PermissionResolver and the #1199
    # write-gate carve-out use — a re-typed literal here is a 3rd copy of the
    # live path that could silently drift from the two the carve-out actually
    # checks (exactly the class of gap #5173 found).
    from reyn.security.permissions.approval_ledger import RELATIVE_PATH

    return project_root / Path(RELATIVE_PATH)


def _legacy_snapshot_path(project_root: Path) -> Path:
    return project_root / ".reyn" / "approvals.yaml"


def _load(project_root: Path) -> dict[str, bool]:
    """Fold the ledger (migrating a legacy snapshot first, if present)."""
    from reyn.security.permissions.approval_ledger import (
        ApprovalLedger,
        migrate_legacy_snapshot,
    )
    ledger = ApprovalLedger(_ledger_path(project_root))
    migrate_legacy_snapshot(ledger, _legacy_snapshot_path(project_root))
    approvals, _bound = ledger.fold()
    return approvals


# ── response models ───────────────────────────────────────────────────────────


class ApprovalEntry(BaseModel):
    key: str
    approved: bool


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/permissions", response_model=list[ApprovalEntry])
async def list_permissions(
    project_root: Path = Depends(get_project_root),
) -> list[ApprovalEntry]:
    """Return all saved approval entries, folded from the ledger."""
    data = _load(project_root)
    return [ApprovalEntry(key=k, approved=v) for k, v in sorted(data.items())]


@router.delete("/permissions/{key:path}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_permission(
    key: str,
    project_root: Path = Depends(get_project_root),
) -> None:
    """Revoke a single approval entry by its key.

    #5065/#5153: this is a management operation on the SAVED-approvals
    store, not an in-run permission decision, so this route emits its
    own audit-event naming the revoked key (the ledger append itself
    carries no semantic "this was a revoke, from the web surface"
    label — the event is the durable, human-facing trail for that).
    Best-effort: the emit runs AFTER the append and swallows its own
    failure (see ``emit_direct_event``'s docstring) — the append always
    lands, but on an emit failure the audit trail does not record it.
    """
    data = _load(project_root)
    if key not in data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No saved approval with key {key!r}.",
        )
    from reyn.security.permissions.approval_ledger import ApprovalLedger
    ApprovalLedger(_ledger_path(project_root)).append_approval(key, False)
    emit_direct_event(
        "permission_approval_revoked",
        surface=_SURFACE,
        reyn_root=project_root / ".reyn",
        key=key,
    )


@router.delete("/permissions", status_code=status.HTTP_204_NO_CONTENT)
async def clear_permissions(
    confirm: bool = Body(default=False, embed=True),
    project_root: Path = Depends(get_project_root),
) -> None:
    """Clear all saved approvals. Requires body: {\"confirm\": true}.

    #5065/#5153: same shape as :func:`revoke_permission` above — a bulk
    clear has no single key to name, so the audit-event carries the
    count of entries actually revoked instead (only the currently-
    APPROVED ones; an already-revoked entry has nothing left to clear).
    """
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Pass {\"confirm\": true} in the request body to clear all approvals.",
        )
    data = _load(project_root)
    currently_approved = [k for k, v in data.items() if v]
    from reyn.security.permissions.approval_ledger import ApprovalLedger
    ledger = ApprovalLedger(_ledger_path(project_root))
    for key in currently_approved:
        ledger.append_approval(key, False)
    emit_direct_event(
        "permission_approvals_cleared",
        surface=_SURFACE,
        reyn_root=project_root / ".reyn",
        count=len(currently_approved),
    )
