"""REST router — /api/runs.

Placeholder — all ``skill_runs``-backed list/detail endpoints were removed
because the ``.reyn/events/.../skill_runs/`` directory is never written by
current code (async skill infrastructure was retired in #2104).

The router object is kept so ``server.py`` import and ``include_router`` lines
need no change, but contributes zero routes.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["runs"])
