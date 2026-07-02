"""Markdown YAML-frontmatter splitting utility.

Splits a Markdown document into its leading ``---``-delimited YAML frontmatter
block and the remaining body. Shared by any subsystem that reads structured
metadata from Markdown files (file tools, memory store).
"""
from __future__ import annotations

import yaml


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a Markdown file into (frontmatter dict, body string).

    Returns ``({}, text)`` unchanged when the text has no leading ``---``
    frontmatter block or the block is never closed.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    if end is None:
        return {}, text
    fm = yaml.safe_load("\n".join(lines[1:end])) or {}
    body = "\n".join(lines[end + 1:]).strip()
    return fm, body
