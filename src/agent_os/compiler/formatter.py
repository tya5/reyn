"""
DSL Formatter

Rewrites DSL files into canonical form:
  - Artifact: required fields first, optional fields last
  - Phase: frontmatter keys in PHASE_FRONTMATTER_ORDER
"""
from __future__ import annotations
from pathlib import Path

from .parser import _split_frontmatter, _parse_fields
from .linter import PHASE_FRONTMATTER_ORDER  # ["type","name","input","input_description","role","can_finish"]


def _yaml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    return str(v)


def _build_frontmatter(fm: dict) -> str:
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, dict):
            lines.append(f"{k}:")
            for sk, sv in v.items():
                lines.append(f"  {sk}: {_yaml_scalar(sv)}")
        elif v is None:
            lines.append(f"{k}:")
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines)


def format_artifact(text: str) -> str:
    """Reorder artifact fields: required first, optional last."""
    fm, body = _split_frontmatter(text)
    fields = _parse_fields(body)

    required = [f for f in fields if not f.optional]
    optional = [f for f in fields if f.optional]

    field_lines: list[str] = []
    for f in required:
        field_lines.append(f"{f.name}: {f.type_str}")
    for f in optional:
        field_lines.append(f"{f.name}?: {f.type_str}")

    new_body = "\n".join(field_lines) if field_lines else ""
    return _build_frontmatter(fm) + "\n\n" + new_body + "\n"


def format_phase(text: str) -> str:
    """Reorder phase frontmatter keys into canonical order."""
    fm, body = _split_frontmatter(text)

    ordered: dict = {}
    for key in PHASE_FRONTMATTER_ORDER:
        if key in fm:
            ordered[key] = fm[key]
    for key in fm:
        if key not in ordered:
            ordered[key] = fm[key]

    body = body.rstrip("\n")
    return _build_frontmatter(ordered) + "\n\n" + body + "\n"


def format_dsl(dsl_root: Path, write: bool = True) -> list[Path]:
    """
    Format all artifacts and phases under dsl_root.
    Returns list of files that were changed (or would change if write=False).
    """
    changed: list[Path] = []

    artifact_dirs: list[Path] = [dsl_root / "shared" / "artifacts"]
    apps_root = dsl_root / "apps"
    if apps_root.exists():
        artifact_dirs += sorted(apps_root.glob("*/artifacts"))

    phase_dirs: list[Path] = [dsl_root / "shared" / "phases"]
    if apps_root.exists():
        phase_dirs += sorted(apps_root.glob("*/phases"))

    for d in artifact_dirs:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            original = p.read_text(encoding="utf-8")
            formatted = format_artifact(original)
            if formatted != original:
                changed.append(p)
                if write:
                    p.write_text(formatted, encoding="utf-8")

    for d in phase_dirs:
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            original = p.read_text(encoding="utf-8")
            formatted = format_phase(original)
            if formatted != original:
                changed.append(p)
                if write:
                    p.write_text(formatted, encoding="utf-8")

    return changed
