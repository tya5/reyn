"""Docs tab — file browser over docs/ Markdown files with cursor highlight."""
from __future__ import annotations

from pathlib import Path

from .base import _CORAL, _TEXT_BODY, _TEXT_DIM, _TEXT_NEUTRAL, _esc

# ---------------------------------------------------------------------------
# Language helpers
# ---------------------------------------------------------------------------

def _base_stem(p: Path) -> str:
    """Return the language-neutral base stem for a docs path.

    ``glossary.ja.md`` → ``"glossary"``
    ``glossary.md``    → ``"glossary"``
    """
    stem = p.stem  # strips the final ".md"
    return stem[:-3] if stem.endswith(".ja") else stem


def _is_ja(p: Path) -> bool:
    """True when the path is the Japanese (.ja.md) variant."""
    return p.stem.endswith(".ja")


def build_docs_index(
    project_root: Path | None,
    docs_filter: str = "",
    *,
    lang: str = "en",
) -> tuple[dict[str, list[Path]], list[Path]]:
    """Scan docs/ and return (groups_by_section, ordered_flat_list).

    Returns ({}, []) when the project root or docs/ is missing.
    When `docs_filter` is non-empty, only files whose stem (case-insensitive)
    contains the filter substring are kept. Sections with no matching files
    are dropped from the groups dict.

    ``lang`` controls which language variant is preferred per concept:

    * ``"en"`` (default) — prefer ``.md``; fall back to ``.ja.md`` when absent.
    * ``"ja"``           — prefer ``.ja.md``; fall back to ``.md`` when absent.

    Each unique base concept appears exactly once in the returned lists.
    The fallback variant is still included but labelled so the caller can
    render a dim suffix (see ``render_docs``).
    """
    if project_root is None:
        return {}, []
    # Try docs/en/ first (i18n layout), then fall back to docs/ directly.
    docs_root = project_root / "docs" / "en"
    if not docs_root.is_dir():
        docs_root = project_root / "docs"
    if not docs_root.is_dir():
        return {}, []

    # Collect all .md files grouped by section.
    raw_groups: dict[str, list[Path]] = {}
    for md in sorted(docs_root.rglob("*.md")):
        rel = md.relative_to(docs_root)
        section = rel.parts[0] if len(rel.parts) > 1 else ""
        raw_groups.setdefault(section, []).append(md)

    # Per section: collapse en/ja pairs to one file per base stem.
    groups: dict[str, list[Path]] = {}
    for section, paths in raw_groups.items():
        # Build base → {True: ja_path, False: en_path} map.
        by_base: dict[str, dict[bool, Path]] = {}
        for p in paths:
            base = _base_stem(p)
            by_base.setdefault(base, {})[_is_ja(p)] = p

        # Pick the preferred variant for each base, in alphabetical base order.
        chosen: list[Path] = []
        for base in sorted(by_base):
            variants = by_base[base]
            if lang == "ja":
                # Prefer Japanese; fall back to English.
                chosen.append(variants.get(True) or variants[False])
            else:
                # Prefer English; fall back to Japanese.
                chosen.append(variants.get(False) or variants[True])
        groups[section] = chosen

    if docs_filter:
        needle = docs_filter.lower()
        filtered: dict[str, list[Path]] = {}
        for section, paths in groups.items():
            kept = [p for p in paths if needle in _base_stem(p).lower()]
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
    lang: str = "en",
) -> str:
    """Render the docs index with the file at ``docs_cursor`` highlighted.

    ``lang`` must be ``"en"`` or ``"ja"`` — controls the preference header
    and which rows are annotated with a dim fallback suffix.
    """
    if project_root is None:
        return f"[{_TEXT_DIM}]  (no project root)[/]"
    # Mirror the fallback logic in build_docs_index.
    docs_root = project_root / "docs" / "en"
    if not docs_root.is_dir():
        docs_root = project_root / "docs"
    if not docs_root.is_dir():
        return f"[{_TEXT_DIM}]  (docs/ not found)[/]"

    lines: list[str] = []

    # Lang preference header — always rendered so the user knows the active
    # setting without having to press ``g``.
    other_lang = "en" if lang == "ja" else "ja"
    lines.append(
        f"[{_TEXT_BODY}]  lang: [/][{_CORAL}]{lang}[/]"
        f"[{_TEXT_DIM}]  ({other_lang} fallback)  \\[g] to toggle[/]"
    )
    lines.append("")

    if docs_filter:
        # A-F4 (wave-8): hint advertises Esc as the direct clear path.
        # Pre-A-F4, the only clear flow was ``/docs-filter`` (no arg) — a
        # 4-step (press `/`, delete prefill, submit empty) that no other
        # filter UI requires. ``RightPanel.on_key`` now clears in place
        # on Esc when ``_docs_filter`` is non-empty.
        lines.append(
            f"[{_TEXT_BODY}]  filter: [/][{_CORAL}]{_esc(docs_filter)}[/]"
            f"[{_TEXT_DIM}]  (Esc to clear)[/]"
        )
        lines.append("")
    if not groups:
        lines.append(f"[{_TEXT_DIM}]  (no matches)[/]")
        return "\n".join(lines)

    file_idx = 0
    for section in sorted(groups):
        label = section.upper() if section else "ROOT"
        lines.append(f"[bold {_TEXT_BODY}]  \\[{_esc(label)}][/]")
        for md in groups[section]:
            indent = "    "
            base = _base_stem(md)
            is_ja_file = _is_ja(md)

            # Determine if this row is a fallback (= preferred lang was absent).
            # lang="ja" + file is NOT ja → fallback → show dim (en) suffix.
            # lang="en" + file IS ja    → fallback → show dim (ja) suffix.
            fallback_suffix = ""
            if lang == "ja" and not is_ja_file:
                fallback_suffix = "  (en)"
            elif lang == "en" and is_ja_file:
                fallback_suffix = "  (ja)"

            if file_idx == docs_cursor:
                lines.append(
                    f"[bold {_CORAL}]{indent}▶ {_esc(base)}[/]"
                    + (f"[dim {_CORAL}]{fallback_suffix}[/]" if fallback_suffix else "")
                )
            else:
                lines.append(
                    f"[{_TEXT_NEUTRAL}]{indent}  {_esc(base)}[/]"
                    + (f"[dim {_TEXT_NEUTRAL}]{fallback_suffix}[/]" if fallback_suffix else "")
                )
            file_idx += 1
        lines.append("")

    return "\n".join(lines)


__all__ = ["build_docs_index", "render_docs"]
