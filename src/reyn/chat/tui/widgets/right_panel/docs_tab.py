"""Docs tab — file browser over docs/en/ Markdown files with cursor highlight."""
from __future__ import annotations

from pathlib import Path

from .base import _CORAL, _esc


def build_docs_index(project_root: Path | None) -> tuple[dict[str, list[Path]], list[Path]]:
    """Scan docs/en/ and return (groups_by_section, ordered_flat_list).

    Returns ({}, []) when the project root or docs/en/ is missing.
    """
    if project_root is None:
        return {}, []
    docs_root = project_root / "docs" / "en"
    if not docs_root.is_dir():
        return {}, []

    groups: dict[str, list[Path]] = {}
    for md in sorted(docs_root.rglob("*.md")):
        rel = md.relative_to(docs_root)
        section = rel.parts[0] if len(rel.parts) > 1 else ""
        groups.setdefault(section, []).append(md)

    ordered: list[Path] = []
    for section in sorted(groups):
        ordered.extend(groups[section])
    return groups, ordered


def render_docs(
    project_root: Path | None,
    docs_cursor: int,
    groups: dict[str, list[Path]],
) -> str:
    """Render the docs index with the file at ``docs_cursor`` highlighted."""
    if project_root is None:
        return "[#555555]  (no project root)[/]"
    docs_root = project_root / "docs" / "en"
    if not docs_root.is_dir():
        return "[#555555]  (docs/en/ not found)[/]"

    lines: list[str] = []
    file_idx = 0
    for section in sorted(groups):
        label = section.upper() if section else "ROOT"
        lines.append(f"[bold #aaaaaa]  \\[{_esc(label)}][/]")
        for md in groups[section]:
            indent = "    "
            if file_idx == docs_cursor:
                lines.append(f"[bold {_CORAL}]{indent}▶ {_esc(md.stem)}[/]")
            else:
                lines.append(f"[#666666]{indent}  {_esc(md.stem)}[/]")
            file_idx += 1
        lines.append("")

    return "\n".join(lines)


__all__ = ["build_docs_index", "render_docs"]
