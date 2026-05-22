"""Docs tab — file browser over docs/ Markdown files with cursor highlight."""
from __future__ import annotations

from pathlib import Path

from .base import _CORAL, _esc


def build_docs_index(
    project_root: Path | None,
    docs_filter: str = "",
) -> tuple[dict[str, list[Path]], list[Path]]:
    """Scan docs/ and return (groups_by_section, ordered_flat_list).

    Returns ({}, []) when the project root or docs/ is missing.
    When `docs_filter` is non-empty, only files whose stem (case-insensitive)
    contains the filter substring are kept. Sections with no matching files
    are dropped from the groups dict.
    """
    if project_root is None:
        return {}, []
    # Try docs/en/ first (i18n layout), then fall back to docs/ directly.
    docs_root = project_root / "docs" / "en"
    if not docs_root.is_dir():
        docs_root = project_root / "docs"
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
    # Mirror the fallback logic in build_docs_index.
    docs_root = project_root / "docs" / "en"
    if not docs_root.is_dir():
        docs_root = project_root / "docs"
    if not docs_root.is_dir():
        return "[#555555]  (docs/ not found)[/]"

    lines: list[str] = []
    if docs_filter:
        # A-F4 (wave-8): hint advertises Esc as the direct clear path.
        # Pre-A-F4, the only clear flow was ``/docs-filter`` (no arg) — a
        # 4-step (press `/`, delete prefill, submit empty) that no other
        # filter UI requires. ``RightPanel.on_key`` now clears in place
        # on Esc when ``_docs_filter`` is non-empty.
        lines.append(
            f"[#aaaaaa]  filter: [/][{_CORAL}]{_esc(docs_filter)}[/]"
            f"[#555555]  (Esc to clear)[/]"
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
            # Wave-4 PC2: stems ending in ``.ja`` (= ``foo.ja.md``)
            # are Japanese translations of the English doc next to
            # them. Without distinction the alphabetical sort
            # interleaves them (``a2a.ja / a2a / care-boundary.ja /
            # care-boundary / …``), confusing for English-default
            # users. Annotate the JA stems with a dim ``(ja)``
            # suffix so the list reads at-a-glance: same alphabetical
            # order, language identified per row. Avoids a deeper
            # restructure (= subsection split / filter UI) for the
            # minimum visible-distinction fix.
            stem = md.stem
            if stem.endswith(".ja"):
                base = stem[:-3]
                if file_idx == docs_cursor:
                    lines.append(
                        f"[bold {_CORAL}]{indent}▶ {_esc(base)}[/]"
                        f"[dim {_CORAL}]  (ja)[/]"
                    )
                else:
                    lines.append(
                        f"[#666666]{indent}  {_esc(base)}[/]"
                        f"[dim #666666]  (ja)[/]"
                    )
            else:
                if file_idx == docs_cursor:
                    lines.append(f"[bold {_CORAL}]{indent}▶ {_esc(stem)}[/]")
                else:
                    lines.append(f"[#666666]{indent}  {_esc(stem)}[/]")
            file_idx += 1
        lines.append("")

    return "\n".join(lines)


__all__ = ["build_docs_index", "render_docs"]
