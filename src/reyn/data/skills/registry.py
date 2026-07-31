"""reyn.data.skills.registry — skill registry model and config-entry builder (#2548 PR-A).

A "skill" = an industry-standard ``SKILL.md`` file in a named directory.
Skills are registered PURELY via explicit ``skills.entries`` declarations
in any config tier — matching the ``mcp.servers`` registration model.
The ``SKILL.md`` body is read by the model at L2 (via the existing
file-read op) when a skill is relevant; the registry never reads it.

``path`` in each entry may be project-root-relative or absolute; it
points at the skill's ``SKILL.md`` / directory. The registry stores it
as-is for L1 system-prompt display.

**The visibility axis (#2971).** ``visibility`` names WHICH discovery
surface a skill reaches, on a single ordered axis — not a pair of
booleans. It replaces the removed ``auto_invoke`` flag, which was a
misnomer: no mechanism has ever auto-invoked a skill, so ``auto_invoke``
only ever meant "render into the L1 menu", and its ``False`` value
collapsed "do not advertise" into "unreachable by anyone" because the
menu was the ONLY surface naming a skill. The three states are exactly
the three an operator can mean:

  - ``menu``     — rendered into the L1 system-prompt Skills menu
                   (``prompt.universal_slots.build_skills_slot``).
  - ``on_demand`` — NOT in the menu, but returned by the ``skill_list``
                   tool: the model learns it exists only when it asks.
                   Zero standing token cost. The state that did not exist
                   before #2971, and the one builtin skills ship in.
  - ``hidden``   — on no model-facing surface at all. The effective
                   behavior ``auto_invoke: false`` delivered.

**``enabled`` dominates ``visibility``.** ``enabled: false`` drops the
entry from the registry entirely (see :func:`build_skill_registry`), so
``visibility`` is only meaningful while ``enabled`` is true. The two
fields therefore describe 4 reachable states, not 2x3=6: one "not
registered" state, plus the three visibility states above.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Maximum characters to keep from a skill description (one-line cap), ELLIPSIS
# INCLUDED — ``skills.entries.<name>.description`` is arbitrary operator/config
# text that reaches the system prompt and the ``skill_list`` result, so the bound
# is what keeps that standing token cost finite.
_DESC_MAX = 200

# Appended when the cap actually CUTS, so the loss is legible on every surface
# the description reaches. A bare ``[:200]`` slice lands mid-word and reads as a
# rendering fault rather than as a cap — #3545 was reported as "the completion
# popup is losing text" when the popup was faithfully rendering every character
# it was handed and the cut had happened here, at load.
_DESC_ELLIPSIS = "…"

# The visibility axis (#2971), ordered widest-reach → narrowest. This tuple is
# the single source of truth: the loader's validation error renders it, and
# ``docs/concepts/tools-integrations/skills.md`` mirrors it.
VISIBILITY_MENU = "menu"
VISIBILITY_ON_DEMAND = "on_demand"
VISIBILITY_HIDDEN = "hidden"
VISIBILITIES: "tuple[str, ...]" = (VISIBILITY_MENU, VISIBILITY_ON_DEMAND, VISIBILITY_HIDDEN)

# Default when an entry omits ``visibility``: the widest reach, matching the
# removed ``auto_invoke``'s ``True`` default (an entry that says nothing is
# advertised, as before).
VISIBILITY_DEFAULT = VISIBILITY_MENU


@dataclass
class SkillEntry:
    """A single skill loaded from explicit config declaration.

    ``path`` is as declared in ``skills.entries.<name>.path`` — may be
    project-root-relative or absolute. The registry stores it as-is; the
    model reads the file at L2 when the skill is relevant.
    ``enabled`` mirrors the config key (default True). ``visibility`` is the
    #2971 three-state discovery axis (``menu`` / ``on_demand`` / ``hidden``,
    default ``menu``) — see the module docstring for why it is one enum and
    not two booleans, and for why ``enabled`` dominates it.
    """
    name: str
    description: str
    path: str
    enabled: bool = True
    visibility: str = VISIBILITY_DEFAULT


def _truncate_description(desc: str) -> str:
    """Keep only the first line, capped to :data:`_DESC_MAX` chars.

    A cut lands on a WORD boundary and carries :data:`_DESC_ELLIPSIS`. Both are
    about making the cap SAY SO: the description is the text a model decides
    relevance from and the text the ``:``-completion popup lists, and a bare
    slice hands both a truncated final word with nothing to distinguish "the
    author wrote this much" from "something downstream broke". The ellipsis is
    inside the budget, not added to it, so the bound this function exists to
    enforce still holds exactly.
    """
    first_line = (desc.splitlines()[0] if desc else "").strip()
    if len(first_line) <= _DESC_MAX:
        return first_line
    head = first_line[: _DESC_MAX - len(_DESC_ELLIPSIS)]
    # Back up to the last space so the visible text ends on a whole word; a
    # single unbroken run longer than the cap has no boundary to find, and is
    # cut where it falls rather than dropped to empty.
    kept = head.rsplit(" ", 1)[0] if " " in head else head
    return kept.rstrip() + _DESC_ELLIPSIS


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
    # Lenient here, as everywhere in this builder: an unknown/absent
    # ``visibility`` falls back to the default rather than raising, because
    # this function is also reached from the best-effort hot-reload re-read.
    # The AUTHORITATIVE rejection of a bad value (and of the removed
    # ``auto_invoke`` key) is ``config.loader._validate_skill_visibility``,
    # which runs at load and raises — see that function's docstring.
    raw_visibility = raw.get("visibility", VISIBILITY_DEFAULT)
    visibility = (
        str(raw_visibility) if str(raw_visibility) in VISIBILITIES else VISIBILITY_DEFAULT
    )
    return SkillEntry(
        name=name,
        description=description,
        path=path,
        enabled=enabled,
        visibility=visibility,
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
