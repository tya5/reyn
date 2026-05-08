"""Docs tab — file browser over docs/en/ Markdown files with cursor highlight."""
from __future__ import annotations

from pathlib import Path

from .base import _CORAL, _esc


def build_docs_index(
    project_root: Path | None,
    docs_filter: str = "",
) -> tuple[dict[str, list[Path]], list[Path]]:
    """Scan docs/en/ and return (groups_by_section, ordered_flat_list).

    Returns ({}, []) when the project root or docs/en/ is missing.
    When `docs_filter` is non-empty, only files whose stem (case-insensitive)
    contains the filter substring are kept. Sections with no matching files
    are dropped from the groups dict.
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

    if docs_filter:
        needle = docs_filter.lower()
        filtered: dict[str, list[Path]] = {}
        for section, paths in groups.items():
            kept = [p for p in paths if needle in p.stem.lower()]
            if kept:
                filtered[section] = kept
        groups = filtered

    ordered: list[Path] = []
    for section in sorted(groups):
        ordered.extend(groups[section])
    return groups, ordered


def render_docs(
    project_root: Path | None,
    docs_cursor: int,
    groups: dict[str, list[Path]],
    *,
    docs_filter: str = "",
) -> str:
    """Render the docs index with the file at ``docs_cursor`` highlighted."""
    if project_root is None:
        return "[#555555]  (no project root)[/]"
    docs_root = project_root / "docs" / "en"
    if not docs_root.is_dir():
        return "[#555555]  (docs/en/ not found)[/]"

    lines: list[str] = []
    if docs_filter:
        lines.append(
            f"[#aaaaaa]  filter: [/][{_CORAL}]{_esc(docs_filter)}[/]"
            f"[#555555]  (clear via /docs-filter)[/]"
        )
        lines.append("")
    if not groups:
        lines.append("[#555555]  (no matches)[/]")
        return "\n".join(lines)

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
