"""Tier 2: prose no longer says "chat-event" — the term is "audit-event" (#3794 P2).

``session_attached`` is an ``EventFrame``, not a chat-event (P1, #3836); P2
finishes the correction for prose describing the REAL audit trail too — "chat-
event" was always the wrong noun for ``.reyn/events`` entries (P1's finding
generalised: reyn never had a chat-scoped event type, only ``EventLog``'s
audit-events). This gate pins that generalisation as a repo-wide invariant so
it cannot silently regress as new prose is added.

Scope matches the P2 design (issue #3794, architect comment): ``src/``,
``tests/``, ``docs/``, and root-level ``.md``/``.yaml``/``.toml``/``.json``,
excluding ``.venv/``, ``site/``, ``.git/``. This gate is prose-only — it does
NOT match the underscore-joined identifier spelling of this term, which was
P3's separate, much larger mechanical-rename scope (602 sites, one dedicated
PR spanning ``src/`` + ``tests/`` together, #3794 P3). Matching identifiers
here would have made this gate RED the moment P2 landed and P3 hadn't, which
was never this gate's job.

``docs/deep-dives/journal/`` is also excluded: it is a dated log of past
dogfood/investigation runs, and its entries are historical statements of what
was observed AT THE TIME (in the vocabulary current then) — the same
exemption the design gives any other "renamed in #NNN"-style past-tense
sentence, just at directory granularity because the whole tree is a frozen
record rather than living prose.

Real repo tree is walked — nothing is faked; a grep-shaped gate over faked
paths would prove nothing about the actual prose.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests._support.paths import REPO_ROOT

EXCLUDED_DIR_NAMES = {".venv", "site", ".git", "__pycache__"}
# Dated dogfood/investigation logs — historical statements, exempt (see
# module docstring).
EXCLUDED_PATH_PREFIXES = (REPO_ROOT / "docs" / "deep-dives" / "journal",)
# The gate itself necessarily quotes the term it's scanning for (docstring,
# positive-control fixture) — exclude it from its own scan, same as any
# self-referential census gate must.
SELF_PATH = Path(__file__).resolve()

# Hyphen or space separator only — underscore (identifier) occurrences are P3
# scope, not this gate's. Trailing \b excludes compounds like "EventStore" /
# "EventLog" ("chat EventStore" is not the chat-event/audit-event term).
_PROSE_PATTERN = re.compile(r"(?i)\bchat[- ]event\b")


def _prose_target_files() -> list[Path]:
    files: list[Path] = []
    for sub in ("src", "tests", "docs"):
        root = REPO_ROOT / sub
        for path in root.rglob("*"):
            if not path.is_file() or path == SELF_PATH:
                continue
            if EXCLUDED_DIR_NAMES & set(path.relative_to(REPO_ROOT).parts):
                continue
            if any(prefix in path.parents for prefix in EXCLUDED_PATH_PREFIXES):
                continue
            files.append(path)
    for path in REPO_ROOT.iterdir():
        if path.is_file() and path.suffix in {".md", ".yaml", ".toml", ".json"}:
            files.append(path)
    return files


def _prose_hits(files: list[Path]) -> list[str]:
    hits = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _PROSE_PATTERN.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    return hits


def test_positive_control_the_scanner_actually_detects_audit_event_prose() -> None:
    """Tier 2: the pattern DOES fire on a known chat-event sentence (guards the gate itself)."""
    known_positive = ["a stray line describing a chat-event delta on the wire"]
    hits = [line for line in known_positive if _PROSE_PATTERN.search(line)]
    assert hits, "the prose pattern failed to match a known chat-event sentence — the gate below would silently report a false zero"


def test_no_audit_event_prose_remains_repo_wide() -> None:
    """Tier 2: zero "chat-event" prose occurrences remain in src/tests/docs + root config (#3794 P2)."""
    hits = _prose_hits(_prose_target_files())
    assert not hits, "found remaining chat-event prose (should read audit-event now):\n" + "\n".join(hits)
