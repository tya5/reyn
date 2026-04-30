"""Memory store helpers — frontmatter-aware reader, indexer, and resolver.

This module centralizes the on-disk format used by `recall_memory` /
`write_memory` skills and the `reyn memory` CLI:

  <memory_dir>/
    MEMORY.md           — index ([Name](slug.md) — description)
    <slug>.md           — body file with frontmatter (name, description, type)

Each callsite that wanted to read or rewrite this layout previously
reimplemented frontmatter splitting and index regeneration locally. This
module is the single source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .compiler.parser import _split_frontmatter
from .memory_paths import global_memory_dir, project_memory_dir


VALID_TYPES = ("user", "feedback", "project", "reference")


@dataclass
class MemoryEntry:
    """A single memory loaded from disk.

    `scope` is "global" / "project" / "?" (unknown when caller didn't supply).
    `slug` is the filename without the .md extension.
    `body` is the prose with frontmatter stripped and surrounding whitespace
    trimmed — ready to inject without further normalization.
    """
    scope: str
    slug: str
    path: Path
    name: str
    description: str
    type: str
    body: str


# ── reading ───────────────────────────────────────────────────────────────────


def read_entry(scope: str, path: Path) -> MemoryEntry | None:
    """Load and parse a memory file. Returns None if unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = _split_frontmatter(text)
    description_raw = str(fm.get("description") or "").strip()
    description = description_raw.splitlines()[0] if description_raw else ""
    return MemoryEntry(
        scope=scope,
        slug=path.stem,
        path=path,
        name=str(fm.get("name") or path.stem),
        description=description,
        type=str(fm.get("type") or ""),
        body=body.strip(),
    )


def list_entries(scope_dirs: list[tuple[str, Path]]) -> list[MemoryEntry]:
    """Read every memory across the given (scope_label, dir) pairs.

    Skips MEMORY.md itself and any unreadable file. Returns entries in
    (scope_dirs order, file-name sort) order.
    """
    out: list[MemoryEntry] = []
    for scope, d in scope_dirs:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            entry = read_entry(scope, f)
            if entry is not None:
                out.append(entry)
    return out


def default_scope_dirs(state_dir: str | Path) -> list[tuple[str, Path]]:
    """Return the canonical [(global, …), (project, …)] dir pair."""
    return [
        ("global", global_memory_dir()),
        ("project", project_memory_dir(state_dir)),
    ]


# ── name resolution ───────────────────────────────────────────────────────────


class AmbiguousMemoryError(Exception):
    """Raised when a query matches more than one memory entry."""

    def __init__(self, query: str, matches: list[MemoryEntry]) -> None:
        super().__init__(f"{query!r} matches {len(matches)} entries")
        self.query = query
        self.matches = matches


def find_one(query: str, entries: list[MemoryEntry]) -> MemoryEntry | None:
    """Resolve a slug, display name, or substring to a single MemoryEntry.

    Match precedence (first non-empty wins):
      1. exact slug
      2. case-insensitive name match
      3. substring match on slug or name

    Returns None if nothing matches. Raises AmbiguousMemoryError if multiple
    candidates are tied at the same precedence level.
    """
    target = query.strip()
    if target.endswith(".md"):
        target = target[:-3]

    exact = [e for e in entries if e.slug == target]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AmbiguousMemoryError(target, exact)

    ci_name = [e for e in entries if e.name.lower() == target.lower()]
    if len(ci_name) == 1:
        return ci_name[0]
    if len(ci_name) > 1:
        raise AmbiguousMemoryError(target, ci_name)

    sub = [
        e for e in entries
        if target.lower() in e.slug.lower() or target.lower() in e.name.lower()
    ]
    if len(sub) == 1:
        return sub[0]
    if len(sub) > 1:
        raise AmbiguousMemoryError(target, sub)

    return None


# ── index regeneration ────────────────────────────────────────────────────────


def rewrite_index(scope_dir: Path) -> None:
    """Rebuild scope_dir/MEMORY.md from the .md files actually present."""
    entries: list[MemoryEntry] = []
    for f in sorted(scope_dir.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        e = read_entry("?", f)
        if e is not None:
            entries.append(e)
    lines = ["# Memory Index", ""]
    for e in entries:
        desc = f" — {e.description}" if e.description else ""
        lines.append(f"- [{e.name}]({e.slug}.md){desc}")
    (scope_dir / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── direct write (used by CLI import; skills do this through Control IR) ──────


def render_body(name: str, description: str, type_: str, body: str) -> str:
    """Render a memory file's full content (frontmatter + body)."""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"type: {type_}\n"
        "---\n\n"
        f"{body.strip()}\n"
    )
