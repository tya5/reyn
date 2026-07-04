"""reyn.data.skills.registry — skill registry model and config-entry builder (#2548 PR-A).

A "skill" = an industry-standard ``SKILL.md`` file in a named directory.
Skills are registered PURELY via explicit ``skills.entries`` declarations
in any config tier — matching the ``mcp.servers`` registration model.
The ``SKILL.md`` body is read by the model at L2 (via the existing
file-read op) when a skill is relevant; the registry never reads it.

``path`` in each entry may be project-root-relative or absolute; it
points at the skill's ``SKILL.md`` / directory. The registry stores it
as-is for L1 system-prompt display.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Maximum characters to keep from a skill description (one-line cap).
_DESC_MAX = 200


@dataclass
class SkillEntry:
    """A single skill loaded from explicit config declaration.

    ``path`` is as declared in ``skills.entries.<name>.path`` — may be
    project-root-relative or absolute. The registry stores it as-is; the
    model reads the file at L2 when the skill is relevant.
    ``enabled`` and ``auto_invoke`` mirror the config keys; both default True.
    """
    name: str
    description: str
    path: str
    enabled: bool = True
    auto_invoke: bool = True


def _truncate_description(desc: str) -> str:
    """Keep only the first line, capped to _DESC_MAX chars."""
    first_line = (desc.splitlines()[0] if desc else "").strip()
    if len(first_line) > _DESC_MAX:
        return first_line[:_DESC_MAX]
    return first_line


def _entry_from_config(name: str, raw: Any) -> SkillEntry | None:
    """Build a SkillEntry from a raw ``skills.entries.<name>`` config dict.

    Returns None when *raw* is not a dict (malformed entry — lenient-default
    pattern matching loader.py).
    """
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip()
    description = _truncate_description(str(raw.get("description") or ""))
    enabled = bool(raw.get("enabled", True))
    auto_invoke = bool(raw.get("auto_invoke", True))
    return SkillEntry(
        name=name,
        description=description,
        path=path,
        enabled=enabled,
        auto_invoke=auto_invoke,
    )


def build_skill_registry(raw_skills: dict) -> list[SkillEntry]:
    """Build the skill registry from merged ``skills:`` config dict.

    ``raw_skills`` is the merged ``skills:`` dict from the config cascade
    (may be empty / absent — lenient). Skills are registered purely via
    explicit ``skills.entries`` declarations; cross-tier union-merge
    (later tier wins on name collision) is handled by ``_merge`` in loader.py
    before this function is called.

    Returns only entries with ``enabled=True``.
    """
    if not isinstance(raw_skills, dict):
        return []

    raw_entries = raw_skills.get("entries")
    if not isinstance(raw_entries, dict):
        return []

    out: list[SkillEntry] = []
    for name, raw in raw_entries.items():
        entry = _entry_from_config(str(name), raw)
        if entry is not None and entry.enabled:
            out.append(entry)
    return out
