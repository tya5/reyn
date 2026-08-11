"""REST router — GET /api/web/config.

Returns the OpenUI design roster and host schema support list.  Scans three
design roots in resolution order (project → local → stdlib-equivalent) and
deduplicates by slug (higher-priority root wins).

Response shape:
    {
        "default_design": "<slug> | null",
        "schemas_supported": ["reyn-ui/v1"],
        "available_designs": [
            {"slug": "...", "source": "project|local|stdlib", "schema": "reyn-ui/v1",
             "faces": ["app", "studio"]}
        ]
    }

Default-design resolution priority:
    1. env REYN_WEB_DEFAULT_DESIGN
    2. reyn.yaml  gateway.default_design (#4317 — was `web.default_design`
       pre-#4174-T4; T4's split enumerated ws_max_size/auth/surfaces but
       dropped this field entirely, leaving it unreadable through the typed
       config for a full cycle)
    3. first available alphabetically

Per P7: no domain-specific strings; design metadata treated as opaque config.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends

from reyn.interfaces.web.deps import get_project_root, get_reyn_config

router = APIRouter(tags=["web"])

# The single schema this host implements.
_SCHEMAS_SUPPORTED = ["reyn-ui/v1"]
_DEFAULT_SCHEMA = "reyn-ui/v1"


# ── helpers ──────────────────────────────────────────────────────────────────


def _design_roots(project_root: Path) -> list[tuple[str, Path]]:
    """Return (source_label, designs_dir) pairs in resolution order.

    project → local → stdlib (web/designs/).
    """
    return [
        ("project", project_root / "reyn" / "project" / "designs"),
        ("local",   project_root / "reyn" / "local"   / "designs"),
        ("stdlib",  project_root / "web"  / "designs"),
    ]


def _read_design_yaml(design_dir: Path) -> dict[str, Any]:
    """Parse design.yaml if present; return {} otherwise."""
    p = design_dir / "design.yaml"
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _collect_designs(project_root: Path) -> list[dict[str, Any]]:
    """Scan all three roots, deduplicate by slug (project > local > stdlib)."""
    roots = _design_roots(project_root)
    seen: set[str] = set()
    results: list[dict[str, Any]] = []

    for source, designs_dir in roots:
        if not designs_dir.is_dir():
            continue
        for slug_dir in sorted(designs_dir.iterdir()):
            if not slug_dir.is_dir():
                continue
            slug = slug_dir.name
            if slug in seen:
                continue  # higher-priority root already provided this slug
            seen.add(slug)

            meta = _read_design_yaml(slug_dir)
            schema = meta.get("schema") or _DEFAULT_SCHEMA
            faces_raw = meta.get("faces")
            if isinstance(faces_raw, list) and faces_raw:
                faces = [str(f) for f in faces_raw]
            else:
                faces = ["app", "studio"]  # best-effort: assume both

            results.append({
                "slug": slug,
                "source": source,
                "schema": schema,
                "faces": faces,
            })

    return results


def _resolve_default(config: Any, available: list[dict]) -> str | None:
    """Resolve the default design slug using priority order.

    #4317: was a raw ``yaml.safe_load`` of ``reyn.yaml`` reading the
    (pre-#4174-T4) ``web.default_design`` key — bypassing the config loader
    entirely. Because that parse never went through the typed schema, T4
    splitting ``web:`` did NOT break it: ``cfg.get("web")["default_design"]``
    kept resolving against the raw YAML dict regardless of what the schema
    considered a known key, so an operator's `web.default_design:` silently
    kept working post-T4 with no schema validation ever seeing it — the
    loader-bypass's real cost wasn't "stopped returning a value", it was
    "should have broken when the schema changed underneath it, and silently
    didn't." Reads ``config.gateway.default_design`` from the already-loaded
    ``ReynConfig`` instead: loader-validated, and the same dependency every
    other router on this app already uses. **This is a genuine behavior
    change**, not a pure bugfix — an operator still on `web.default_design:`
    now gets nothing here (falls through to env var / alphabetical) instead
    of the old raw-read value; `reyn config validate`/`migrate` surface the
    rename via the `"web"` `RenamedKeyHint` in `config_schema.py` (already
    covers `default_design` alongside `auth`/`ws_max_size`/`surfaces`).
    """
    # 1. env var
    env_val = os.environ.get("REYN_WEB_DEFAULT_DESIGN", "").strip()
    if env_val:
        return env_val

    # 2. reyn.yaml  gateway.default_design
    val = getattr(getattr(config, "gateway", None), "default_design", None)
    if val:
        return val

    # 3. first alphabetically
    if available:
        return sorted(available, key=lambda d: d["slug"])[0]["slug"]

    return None


# ── route ─────────────────────────────────────────────────────────────────────


@router.get("/web/config")
async def web_config(
    project_root: Path = Depends(get_project_root),
    config: Any = Depends(get_reyn_config),
) -> dict:
    """Return the OpenUI design roster and host schema capabilities."""
    available = _collect_designs(project_root)
    default_design = _resolve_default(config, available)

    return {
        "default_design": default_design,
        "schemas_supported": _SCHEMAS_SUPPORTED,
        "available_designs": available,
    }
