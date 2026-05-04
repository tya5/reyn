"""Memory store helpers — frontmatter-aware reader, indexer, and resolver.

This module centralizes the on-disk format used by `skill_router` (which
writes memories inline as it routes) and the `reyn memory` CLI:

  <memory_dir>/
    MEMORY.md           — index ([Name](slug.md) — description)
    <slug>.md           — body file with frontmatter (name, description, type)

The router LLM emits `file/write` Control IR ops following this format. The
helpers here are used by the `reyn memory` CLI for read paths and by import
tooling for direct writes that bypass the LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reyn.compiler.parser import _split_frontmatter
from reyn.memory.memory_paths import memory_dir


VALID_TYPES = ("user", "feedback", "project", "reference")


@dataclass
class MemoryEntry:
    """A single memory loaded from disk.

    `slug` is the filename without the .md extension.
    `body` is the prose with frontmatter stripped and surrounding whitespace
    trimmed — ready to inject without further normalization.
    """
    slug: str
    path: Path
    name: str
    description: str
    type: str
    body: str


# ── reading ───────────────────────────────────────────────────────────────────


def read_entry(path: Path) -> MemoryEntry | None:
    """Load and parse a memory file. Returns None if unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = _split_frontmatter(text)
    description_raw = str(fm.get("description") or "").strip()
    description = description_raw.splitlines()[0] if description_raw else ""
    return MemoryEntry(
        slug=path.stem,
        path=path,
        name=str(fm.get("name") or path.stem),
        description=description,
        type=str(fm.get("type") or ""),
        body=body.strip(),
    )


def list_entries(scope_dir: Path | None = None) -> list[MemoryEntry]:
    """Read every memory in the project memory dir.

    Skips MEMORY.md itself and any unreadable file. Returns entries sorted by
    file name. `scope_dir` defaults to the project memory dir.
    """
    d = Path(scope_dir) if scope_dir is not None else memory_dir()
    if not d.exists():
        return []
    out: list[MemoryEntry] = []
    for f in sorted(d.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        entry = read_entry(f)
        if entry is not None:
            out.append(entry)
    return out


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

# Memory-specific format constants. Kept here (NOT in op_runtime, per P7) so
# the OS layer stays format-agnostic. The classify phase prompt embeds the
# same constants when emitting `file/regenerate_index` ops, and the CLI calls
# `rewrite_index` below — both paths produce byte-identical MEMORY.md files.
INDEX_FILENAME = "MEMORY.md"
INDEX_HEADER = "# Memory Index\n\n"
ENTRY_TEMPLATE = "- [{name}]({slug}.md) — {description}"


def rewrite_index(scope_dir: Path) -> None:
    """Rebuild scope_dir/MEMORY.md from the .md files actually present.

    Thin wrapper over the parameterized `regenerate_index_impl` helper in
    `op_runtime.file`. Both the LLM-driven Control IR path and the CLI
    mutation paths share the same traversal + format here so on-disk
    indexes never drift between the two writers.
    """
    from reyn.op_runtime.file import regenerate_index_impl
    regenerate_index_impl(
        dir_path=scope_dir,
        output_path=scope_dir / INDEX_FILENAME,
        entry_template=ENTRY_TEMPLATE,
        header=INDEX_HEADER,
    )


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
