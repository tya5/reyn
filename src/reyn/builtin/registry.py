"""reyn.builtin.registry — the builtin-tier config builder (proposal 0060
Phase 1 F3a, Addendum A1/A3/A9).

Mirrors ``reyn.hooks.schema_registry.BUILTIN_HOOK_SCHEMAS``: a code-shipped,
versioned-with-reyn, operator-non-editable constant, but for the three
part-types that never had one (skills / pipelines / presentations —
Addendum A1). :func:`build_builtin_config` is called ONCE by
``reyn.config.loader.load_config`` and merged as the LOWEST config tier —
below ``~/.reyn/config.yaml``, ``reyn.yaml``, ``reyn.local.yaml``, and every
``.reyn/config/*.yaml`` dynamic file — so any operator declaration with the
same entry name silently wins (last-tier-wins-per-name, the same union-merge
``reyn.config.loader._merge`` already applies to every other tier).

**The builtin-provenance seam (A9).** ``provenance="builtin"`` is stamped
HERE, at registry-build / config-load time — structurally distinct from the
install-op seam (``reyn.core.op_runtime.context.provenance_from_ctx``, which
reads ``ctx.turn_origin`` and can only ever produce ``"user_directed"`` or
``"auto_improvement"``). No install op — and therefore no LLM-driven code
path — can ever produce ``provenance="builtin"``: the value is not a field
either op schema exposes, and this stamping function is only ever invoked
from ``load_config``, never from an op handler. A builtin entry can NEVER be
``user_directed``/``auto_improvement`` for the same reason a
``ctx.turn_origin``-derived entry can never be ``builtin``: the two
provenance values are written at two disjoint code paths that share no
runtime call graph.

**Inert-by-construction shipping (A3).** A builtin skill entry has its
``auto_invoke`` forced to ``False`` HERE — unconditionally, regardless of
what ``BUILTIN_SKILLS`` declares — so a builtin skill can never auto-invoke
by default; it is discoverable (``enabled`` stays whatever the entry
declares, default ``True``) but requires an explicit invocation. Pipelines
and presentations need no equivalent force: both are invoke-by-name
(``PipelineRegistry`` / ``PresentationRegistry`` never self-trigger), so
registering one is already inert until something names it (Addendum A3) —
forcing an ``auto_invoke``-shaped field on them would invent state that does
not exist in their schemas.

F3a (this PR) ships ``BUILTIN_SKILLS`` / ``BUILTIN_PIPELINES`` /
``BUILTIN_PRESENTATIONS`` EMPTY — the exemplar content is F3b (proposal §3
F3, a later phase). ``build_builtin_config()`` on an empty registry returns
three empty ``entries`` dicts, a no-op merge (byte-identical to pre-F3a
config resolution) — this is what "ships inert" means at the mechanism
level: zero behavior change until content lands.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# The builtin content maps — EMPTY in F3a (mechanism only). F3b populates
# these with the actual exemplar skills/pipelines/presentations (proposal §3
# F3: one canonical exemplar per axis + one through-chain composition).
# Shape mirrors the operator config entry shape exactly:
#   BUILTIN_SKILLS = {"<name>": {"description": "...", "path": "...", "enabled": True, "auto_invoke": True}}
#   BUILTIN_PIPELINES = {"<key>": {"path": "...", "description": "...", "enabled": True}}
#   BUILTIN_PRESENTATIONS = {"<name>": {"blueprint": {...}, "enabled": True}}
# ---------------------------------------------------------------------------
BUILTIN_SKILLS: "dict[str, dict[str, Any]]" = {}
BUILTIN_PIPELINES: "dict[str, dict[str, Any]]" = {}
BUILTIN_PRESENTATIONS: "dict[str, dict[str, Any]]" = {}


def _stamp_builtin_entry(entry: "dict[str, Any]", *, force_auto_invoke_false: bool) -> "dict[str, Any]":
    """Return *entry* with ``provenance="builtin"`` stamped (A9 seam).

    ``force_auto_invoke_false`` is set for skills only (A3 inert-ship — see
    module docstring); pipelines/presentations have no ``auto_invoke`` field
    to force (their inertness is structural, not flag-based).
    """
    stamped = {**entry, "provenance": "builtin"}
    if force_auto_invoke_false:
        stamped["auto_invoke"] = False
    return stamped


def _stamp_builtin_entries(
    raw_entries: "dict[str, dict[str, Any]]", *, force_auto_invoke_false: bool = False,
) -> "dict[str, dict[str, Any]]":
    """Stamp ``provenance="builtin"`` onto every entry in *raw_entries*."""
    return {
        name: _stamp_builtin_entry(entry, force_auto_invoke_false=force_auto_invoke_false)
        for name, entry in raw_entries.items()
    }


def build_builtin_config() -> "dict[str, Any]":
    """Build the builtin-tier config dict — merged as the LOWEST tier in
    ``reyn.config.loader.load_config`` (below every operator config file).

    Returns the same ``{"skills": {"entries": {...}}, "pipelines": {"entries":
    {...}}, "presentations": {"entries": {...}}}`` shape ``_load_yaml`` would
    hand back for any other config source, so ``load_config`` merges it
    through the existing ``_merge`` union-per-name branches with no special
    casing. Every entry carries ``provenance="builtin"`` (A9); skill entries
    additionally have ``auto_invoke`` forced ``False`` (A3).

    F3a ships ``BUILTIN_SKILLS``/``BUILTIN_PIPELINES``/``BUILTIN_PRESENTATIONS``
    empty, so this returns three empty ``entries`` dicts — a no-op merge.
    """
    return {
        "skills": {"entries": _stamp_builtin_entries(BUILTIN_SKILLS, force_auto_invoke_false=True)},
        "pipelines": {"entries": _stamp_builtin_entries(BUILTIN_PIPELINES)},
        "presentations": {"entries": _stamp_builtin_entries(BUILTIN_PRESENTATIONS)},
    }
