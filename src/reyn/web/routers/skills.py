"""REST router — /api/skills.

Surfaces the three-layer skill resolution tree (project / local / stdlib)
without interpreting skill-domain content. The parsed graph, phases, and
artifact schemas are passed through as opaque dicts (P7).

Routes:
    GET /api/skills             — list all skills (project + local + stdlib)
    GET /api/skills/{name}      — detail: skill.md frontmatter + graph + phases
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from reyn.web.deps import get_project_root

router = APIRouter(tags=["skills"])


# ── helpers ──────────────────────────────────────────────────────────────────


def _search_roots(project_root: Path) -> list[tuple[str, Path]]:
    """Return (source_label, skills_dir) triples in resolution order."""
    from reyn.skill.skill_paths import stdlib_root
    sl = stdlib_root()
    return [
        ("project", project_root / "reyn" / "project"),
        ("local",   project_root / "reyn" / "local"),
        ("stdlib",  sl / "skills"),
    ]


def _read_skill_md(skill_md: Path) -> tuple[dict, str]:
    from reyn.compiler.parser import _split_frontmatter
    try:
        fm, body = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
        return fm, body
    except Exception:
        return {}, ""


def _list_phases(skill_dir: Path) -> list[str]:
    phases_dir = skill_dir / "phases"
    if not phases_dir.is_dir():
        return []
    return sorted(
        p.stem for p in phases_dir.glob("*.md") if p.is_file()
    )


def _list_artifacts(skill_dir: Path) -> list[dict[str, Any]]:
    artifacts_dir = skill_dir / "artifacts"
    if not artifacts_dir.is_dir():
        return []
    import yaml
    result = []
    for f in sorted(artifacts_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            result.append({"file": f.name, **data})
        except Exception:
            result.append({"file": f.name})
    return result


# ── response models ───────────────────────────────────────────────────────────


class SkillSummary(BaseModel):
    name: str
    source: str          # "project" | "local" | "stdlib"
    entry_phase: str | None
    phase_count: int
    description: str | None


class SkillDetail(BaseModel):
    name: str
    source: str
    frontmatter: dict[str, Any]   # raw skill.md frontmatter (opaque pass-through)
    body: str                     # skill.md body text
    phases: list[str]             # phase file stems
    artifacts: list[dict[str, Any]]  # parsed artifact YAML (opaque)
    graph: dict[str, Any]         # frontmatter["graph"] or {} (opaque pass-through)


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/skills", response_model=list[SkillSummary])
async def list_skills(
    project_root: Path = Depends(get_project_root),
) -> list[SkillSummary]:
    """List all skills across project / local / stdlib layers."""
    roots = _search_roots(project_root)
    results: list[SkillSummary] = []
    seen: set[str] = set()

    for source, skills_dir in roots:
        if not skills_dir.is_dir():
            continue
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir() or not (skill_dir / "skill.md").exists():
                continue
            name = skill_dir.name
            fm, _ = _read_skill_md(skill_dir / "skill.md")
            phases = _list_phases(skill_dir)
            results.append(SkillSummary(
                name=name,
                source=source,
                entry_phase=fm.get("entry") or None,
                phase_count=len(phases),
                description=(fm.get("description") or "").strip() or None,
            ))
            seen.add(name)

    return results


@router.get("/skills/{name}", response_model=SkillDetail)
async def get_skill(
    name: str,
    project_root: Path = Depends(get_project_root),
) -> SkillDetail:
    """Return full detail for a named skill."""
    roots = _search_roots(project_root)
    for source, skills_dir in roots:
        skill_dir = skills_dir / name
        skill_md = skill_dir / "skill.md"
        if skill_md.exists():
            fm, body = _read_skill_md(skill_md)
            return SkillDetail(
                name=name,
                source=source,
                frontmatter=fm,
                body=body,
                phases=_list_phases(skill_dir),
                artifacts=_list_artifacts(skill_dir),
                graph=fm.get("graph") or {},
            )

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Skill {name!r} not found.",
    )
