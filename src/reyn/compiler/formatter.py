"""
DSL Formatter

Rewrites phase files into canonical frontmatter order.
Artifact files are plain JSON Schema — no structural reformatting is needed.
"""
from __future__ import annotations
from pathlib import Path

from .parser import _split_frontmatter
from .linter import PHASE_FRONTMATTER_ORDER


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
    Format all phases under dsl_root.
    Returns list of files that were changed (or would change if write=False).
    """
    changed: list[Path] = []

    phase_dirs: list[Path] = [dsl_root / "shared" / "phases"]
    apps_root = dsl_root / "apps"
    if apps_root.exists():
        phase_dirs += sorted(apps_root.glob("*/phases"))

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
