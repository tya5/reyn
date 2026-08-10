"""Tier 1: contract — every relative link in a repo-ROOT markdown file points
at a path that exists.

The root ``README.md`` is not in ``.mkdocs/mkdocs.yml``'s nav, so
``mkdocs build --strict`` — the gate that catches this class everywhere under
``docs/`` — never renders it and never resolves its links. Nothing else did
either: ``test_3126_doc_anchor_gate.py`` validates the ``#anchor`` half of a
reference (does the heading slug exist), not the path half (does the file
exist), and its scopes are ``src/reyn/`` comments and ``docs/`` internals.

The uncovered gap was not hypothetical. Three README links were dead when this
gate was written, the oldest for over a month:

- ``docs/guide/for-skill-authors/`` — removed by #2493
- ``docs/concepts/architecture/architecture.md`` — removed by #2447
- ``docs/concepts/architecture/principles.md`` — removed by #2447

Each was deleted by a purge PR that had no reason to look at the README, and
no gate spanned the two. They surfaced only when an LLM read the README during
a live session, followed the dead directory link, and built a plausible file
path underneath it — so the failure reached a user as "the document does not
exist" rather than as a broken link anyone had clicked.

Scope is deliberately the root and only the root: everything under ``docs/``
already has the mkdocs gate, and duplicating it here would mean two gates
disagreeing about excluded paths.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests._support.paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT

# ``[text](target)`` — skip absolute URLs, mail links, and pure ``#anchor``
# references (the latter are test_3126's scope, not this gate's).
_LINK_RE = re.compile(r"\]\((?!https?://|mailto:|#)([^)\s]+)")


def _root_markdown_files() -> "list[Path]":
    """Enumerated from the live filesystem, never a curated list — a markdown
    file added to the root is covered the moment it lands."""
    return sorted(p for p in _REPO_ROOT.glob("*.md") if p.is_file())


def test_root_markdown_files_are_actually_present() -> None:
    """Tier 1: the gate has something to check.

    A glob that silently matched nothing would make every assertion below
    vacuously true — the shape this repo keeps finding in coverage gates.
    """
    names = {p.name for p in _root_markdown_files()}

    assert "README.md" in names, (
        f"README.md not found at the repo root; the gate is looking in the "
        f"wrong place (found: {sorted(names)})"
    )


def test_every_relative_link_in_root_markdown_resolves() -> None:
    """Tier 1: no root markdown file links to a path that does not exist."""
    dead: "list[str]" = []

    for md in _root_markdown_files():
        text = md.read_text(encoding="utf-8")
        for match in _LINK_RE.finditer(text):
            target = match.group(1).split("#", 1)[0]
            if not target:
                continue  # was a bare "#anchor" with a trailing fragment
            resolved = (md.parent / target).resolve()
            if not resolved.exists():
                line = text.count("\n", 0, match.start()) + 1
                dead.append(f"{md.name}:{line} -> {target}")

    assert not dead, "root markdown links pointing at paths that do not exist:\n" + "\n".join(
        f"  {d}" for d in dead
    )
