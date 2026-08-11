"""Verify a text-level config migrate rewrite before it is trusted (#4295).

``migrate_text.rewrite_text`` only ever moves the specific lines a rename
targets — everything else in the file is untouched by construction. But
"untouched by construction" is a claim about the CODE, not a proof about a
given INPUT file; a shape the line-based extractor didn't anticipate could
still silently produce a value-losing result. This module is the
independent check: re-parse the rewritten text and the original text, apply
the SAME rename mapping to the original's PARSED structure (the same
dotted-path move ``_migrate`` already used before #4295), and assert the
two are equal, key for key. A mismatch means "don't trust this rewrite" —
the caller falls back to reporting the affected keys as needing manual
migration rather than writing a file that might be silently wrong.
"""
from __future__ import annotations

import yaml


def _pop_dotted(d: dict, dotted: str):
    parts = dotted.split(".")
    node = d
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return False, None
    return True, node.pop(parts[-1])


def _set_dotted(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = d
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def verify_rewrite(
    original_text: str, rewritten_text: str, renames: dict[str, str],
) -> bool:
    """True iff applying `renames` structurally to `original_text`'s parsed
    form matches what `rewritten_text` actually parses to. Malformed YAML on
    either side (should never happen — `rewrite_text` only moves lines, it
    doesn't invent syntax) is treated as a verification FAILURE, not an
    exception the caller must separately guard."""
    try:
        original = yaml.safe_load(original_text) or {}
        rewritten = yaml.safe_load(rewritten_text) or {}
    except Exception:
        return False
    if not isinstance(original, dict) or not isinstance(rewritten, dict):
        return False

    expected = dict(original)
    for old_key, new_key in renames.items():
        found, value = _pop_dotted(expected, old_key)
        if not found:
            continue
        _set_dotted(expected, new_key, value)

    return expected == rewritten
