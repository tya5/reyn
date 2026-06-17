"""/concept [<term>] — inline glossary lookup for TUI vocabulary.

Surfaces definitions from ``docs/guide/for-skill-authors/glossary.md``
without leaving the chat pane.  The glossary uses Markdown tables with
``| English | 日本語 | Definition |`` rows (and some two-column variant
tables); the parser walks all tables and captures every term → definition
pair it finds.

Usage::

    /concept              # shows intro + path to full glossary
    /concept skill        # exact match → inline definition
    /concept SKILL        # case-insensitive
    /concept skil         # typo → "did you mean skill, …?" via difflib
    /concept zxzxzx       # unknown → "no glossary entry" + 0 suggestions
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from reyn.interfaces.slash import reply, reply_error, slash

# Canonical path relative to the project root.  Resolved at call time so
# tests can inject a custom path via the ``_glossary_path`` kwarg on the
# internal helpers.
_GLOSSARY_REL = Path("docs/guide/for-skill-authors/glossary.md")


def _project_root() -> Path:
    """Return the project root (= ancestor of ``src/reyn``)."""
    return Path(__file__).parent.parent.parent.parent


def _default_glossary_path() -> Path:
    return _project_root() / _GLOSSARY_REL


# ── parser ────────────────────────────────────────────────────────────────


def _parse_glossary(text: str) -> dict[str, str]:
    """Parse a Markdown glossary text → ``{term_lower: definition}``.

    Handles the two table shapes found in the real glossary:

    1. Three-column ``| English | 日本語 | Definition |``  — the English
       term is column 0, definition is column 2.
    2. Two-column ``| File / Term | Purpose / Meaning |`` — column 0 is
       the term, column 1 is the definition.

    Separator rows (``|---|---|``) and header rows (matching known header
    labels such as "English", "File", "Verb", "Mode", "Word") are skipped.
    Rows whose first column is backtick-wrapped code (e.g. `_default`) are
    included — the backticks are stripped for the lookup key but preserved
    in the stored definition line.

    Returns a ``dict`` whose keys are lower-cased English terms and whose
    values are the raw definition strings (stripped).
    """
    _HEADER_FIRST_COLS = {
        "english", "file", "verb", "mode", "word", "term",
    }
    result: dict[str, str] = {}

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        # Split on | and discard empty border fragments
        parts = [p.strip() for p in stripped.split("|")]
        # Split produces leading/trailing empty strings around the outer |
        parts = [p for p in parts if p != ""]
        if len(parts) < 2:
            continue
        # Skip separator rows  (e.g. |---|------|------------|)
        if re.match(r"^[-:]+$", parts[0]):
            continue
        col0 = parts[0]
        # Strip backtick code spans for key derivation
        key_raw = col0.strip("`").strip()
        if not key_raw:
            continue
        # Skip header rows by checking lower-cased first token
        if key_raw.lower() in _HEADER_FIRST_COLS:
            continue
        # Determine definition column
        if len(parts) >= 3:
            # Three-column table: col2 = definition
            definition = parts[2].strip()
        else:
            # Two-column table: col1 = definition / purpose
            definition = parts[1].strip()

        if not definition:
            continue

        key = key_raw.lower()
        # First occurrence wins (glossary may repeat a term across sections)
        if key not in result:
            result[key] = definition

    return result


# ── public helpers (also used by tests) ───────────────────────────────────


def _load_glossary(path: Path) -> dict[str, str] | None:
    """Read and parse the glossary file.  Returns ``None`` on I/O error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_glossary(text)


def _lookup(
    term: str,
    glossary: dict[str, str],
) -> tuple[str | None, list[str]]:
    """Return ``(definition_or_None, fuzzy_suggestions)``.

    Case-insensitive exact match first.  On miss, ``difflib`` yields up to
    3 close matches from the glossary keys (displayed as original-case).
    """
    key = term.lower()
    if key in glossary:
        return glossary[key], []
    suggestions = difflib.get_close_matches(key, glossary.keys(), n=3, cutoff=0.5)
    return None, list(suggestions)


# ── slash command ──────────────────────────────────────────────────────────

_GLOSSARY_PATH_HINT = (
    "Full glossary: Ctrl+B → Docs → guide/for-skill-authors/glossary"
)


@slash(
    "concept",
    summary="Look up a TUI/reyn concept in the glossary",
    usage="/concept [<term>]",
)
async def concept_cmd(session: object, args: str) -> None:  # noqa: D401
    term = (args or "").strip()

    if not term:
        msg = (
            "Looks up TUI concepts in the glossary. "
            "Try /concept skill or /concept plan.\n"
            f"{_GLOSSARY_PATH_HINT}"
        )
        await reply(session, msg)
        return

    gloss_path = _default_glossary_path()
    glossary = _load_glossary(gloss_path)
    if glossary is None:
        await reply_error(
            session,
            f"glossary unreadable — expected at {_GLOSSARY_REL}",
        )
        return

    definition, suggestions = _lookup(term, glossary)
    if definition is not None:
        await reply(session, f"{term}: {definition}")
        return

    # Miss — compose "did you mean" line
    if suggestions:
        did_you_mean = "did you mean: " + ", ".join(suggestions)
        msg = f"no glossary entry for '{term}' — {did_you_mean}"
    else:
        msg = f"no glossary entry for '{term}'"
    await reply_error(session, msg)
