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

F3a shipped ``BUILTIN_SKILLS`` / ``BUILTIN_PIPELINES`` / ``BUILTIN_PRESENTATIONS``
EMPTY (mechanism only) — ``build_builtin_config()`` on that empty registry
returned three empty ``entries`` dicts, a no-op merge (byte-identical to
pre-F3a config resolution): this is what "ships inert" means at the
mechanism level, zero behavior change until content lands. F3b (proposal §3
F3, Addendum D9.5's curated-5) populated the maps with the exemplar content
across two PRs: the core spine (``reyn_cheat_sheet`` skill + the ``flagship``
pipeline, #2912) and this sibling PR's remaining two exemplars
(``draft_judge_revise`` skill + the ``status_card`` presentation). Every
entry still carries ``provenance="builtin"`` and ships inert (A3) — the
mechanism guarantee is unchanged, only the content maps are no longer empty.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# The builtin content maps. F3b populates the SKILLS + PIPELINES maps with
# the curated core-spine content (proposal 0060 Addendum D6: the "reyn cheat
# sheet" skill is THE flagship builtin — the gap-filler between "reyn has
# these parts" and "the LLM uses them" — plus the flagship through-chain
# pipeline it documents). This F3b sibling PR adds the 2 remaining curated-5
# exemplars (Addendum D9.5 #3/#4): the `draft_judge_revise` workflow skill
# (Evaluation idiom) and the `status_card` present-view (the status/results
# card, invoke-by-name, zero-token exemplar). Shape mirrors the operator
# config entry shape exactly:
#   BUILTIN_SKILLS = {"<name>": {"description": "...", "path": "...", "enabled": True, "auto_invoke": True}}
#   BUILTIN_PIPELINES = {"<key>": {"path": "...", "description": "...", "enabled": True}}
#   BUILTIN_PRESENTATIONS = {"<name>": {"blueprint": {...}, "enabled": True}}
#
# ``path`` is computed ABSOLUTE, relative to THIS module's own file location
# (not project-root-relative) — the builtin content ships inside the
# installed package (the ``builtin/**/*`` package-data glob, F3a Addendum A1),
# physically outside any given user's project_root, so a project-relative
# path would not resolve. (Known follow-up, not solved here: reading a
# builtin skill's SKILL.md body at L2 still passes through the standard
# out-of-project-root file-read permission gate, same as reading any other
# path outside project_root — this PR does not add a builtin-tier carve-out
# to that gate; see the PR body's "known gaps" note.)
# ---------------------------------------------------------------------------
_BUILTIN_DIR = Path(__file__).parent

BUILTIN_SKILLS: "dict[str, dict[str, Any]]" = {
    "reyn_cheat_sheet": {
        "description": (
            "Reyn-specific usage cheat sheet -- which mechanism to reach for "
            "(skill/pipeline/mcp/hook/present), composition idioms, op "
            "essentials, and pointers to the full specs. Read this before "
            "authoring a new part or composing several."
        ),
        "path": str(_BUILTIN_DIR / "skills" / "reyn_cheat_sheet" / "SKILL.md"),
        "enabled": True,
        # auto_invoke is force-stamped False for every builtin skill by
        # _stamp_builtin_entry (A3) regardless of what's declared here —
        # kept explicit for readability, not because it changes anything.
        "auto_invoke": False,
    },
    "draft_judge_revise": {
        "description": (
            "Draft an artifact, self-review it against your own checklist "
            "via a schema-validated agent step, and revise on failure -- the "
            "standard Evaluation-gated workflow for any 'produce then check "
            "quality' task (a summary, a doc section, an email). Read this "
            "before handing off a self-authored artifact you have not gated."
        ),
        "path": str(_BUILTIN_DIR / "skills" / "draft_judge_revise" / "SKILL.md"),
        "enabled": True,
        "auto_invoke": False,
    },
}
BUILTIN_PIPELINES: "dict[str, dict[str, Any]]" = {
    "flagship": {
        "description": (
            "web_search -> agent (summarize) -> agent (self-review, "
            "schema-validated) -> present (zero-token operator output) -- "
            "the through-chain composition thesis exemplar (proposal 0060 F3)."
        ),
        "path": str(_BUILTIN_DIR / "pipelines" / "flagship_research_and_report.yaml"),
        "enabled": True,
    },
    # FP-0063 P3 -- builtin user RAG (proposal 0063). Both pipelines
    # call the P2 builtin MCP servers (vector_store_server / chunker_server,
    # #2952) plus a third-party markitdown MCP server -- all of which ship
    # INERT (R3, mirrors the skill A3 posture): registering these two
    # pipelines is itself inert (invoke-by-name only, Addendum A3), and
    # every step's MCP calls additionally fail cleanly with a decision-
    # enabling message (X1) until the operator explicitly configures +
    # grants the three servers (docs/cookbook/configs/with-builtin-rag-mcp.yaml).
    "rag_ingest": {
        "description": (
            "RAG ingest: chunk -> embed -> store a file or folder into a "
            "user-named sqlite vector store, incrementally by content_hash "
            "(add/update/remove). Requires `python3` on PATH to be reyn's "
            "own interpreter (it shells out; step 0 pre-flights this) -- "
            "proposal 0063 P3."
        ),
        "path": str(_BUILTIN_DIR / "pipelines" / "rag_ingest.yaml"),
        "enabled": True,
    },
    "rag_query": {
        "description": (
            "RAG query: embed the query text and return the top-k nearest "
            "chunks from a sqlite vector store rag_ingest wrote to "
            "-- proposal 0063 P3."
        ),
        "path": str(_BUILTIN_DIR / "pipelines" / "rag_query.yaml"),
        "enabled": True,
    },
}
BUILTIN_PRESENTATIONS: "dict[str, dict[str, Any]]" = {
    # The status/results card exemplar (proposal 0060 Addendum D9.5 curated-5
    # #4): a declarative blueprint -- fixed component set + `$bind` JSON
    # Pointer, zero token cost -- rendering "show a result" as a card rather
    # than as prose. Invoke by name: present(view="status_card",
    # data_inline={"title": "...", "status": "...", "summary": "...",
    # "duration": "..."}); any of the 4 fields may be omitted -- a missing
    # $bind target soft-skips at render (present.md), it does not error.
    "status_card": {
        "description": (
            "Status/results card -- a zero-token present blueprint showing "
            "a title, status, summary, and duration as a compact card "
            "instead of prose. Invoke by name: present(view='status_card', "
            "data_inline={'title': ..., 'status': ..., 'summary': ..., "
            "'duration': ...})."
        ),
        "blueprint": [
            {"component": "markdown", "text": {"$bind": "/title"}},
            {
                "component": "keyvalue",
                "rows": [
                    {"label": "status", "value": {"$bind": "/status"}},
                    {"label": "summary", "value": {"$bind": "/summary"}},
                    {"label": "duration", "value": {"$bind": "/duration"}},
                ],
            },
        ],
        "enabled": True,
    },
}


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
