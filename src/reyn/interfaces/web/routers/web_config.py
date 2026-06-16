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
    2. reyn.yaml  web.default_design
    3. first available alphabetically

Per P7: no skill-specific strings; design metadata treated as opaque config.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends

from reyn.interfaces.web.deps import get_project_root

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


def _resolve_default(project_root: Path, available: list[dict]) -> str | None:
    """Resolve the default design slug using priority order."""
    # 1. env var
    env_val = os.environ.get("REYN_WEB_DEFAULT_DESIGN", "").strip()
    if env_val:
        return env_val

    # 2. reyn.yaml  web.default_design
    reyn_yaml = project_root / "reyn.yaml"
    if reyn_yaml.exists():
        try:
            cfg = yaml.safe_load(reyn_yaml.read_text(encoding="utf-8")) or {}
            if isinstance(cfg, dict):
                web_cfg = cfg.get("web") or {}
                if isinstance(web_cfg, dict):
                    val = web_cfg.get("default_design", "").strip()
                    if val:
                        return val
        except Exception:
            pass

    # 3. first alphabetically
    if available:
        return sorted(available, key=lambda d: d["slug"])[0]["slug"]

    return None


# ── route ─────────────────────────────────────────────────────────────────────


@router.get("/web/config")
async def web_config(
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Return the OpenUI design roster and host schema capabilities."""
    available = _collect_designs(project_root)
    default_design = _resolve_default(project_root, available)

    return {
        "default_design": default_design,
        "schemas_supported": _SCHEMAS_SUPPORTED,
        "available_designs": available,
    }
