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
``visibility`` forced to ``"on_demand"`` HERE — unconditionally, regardless
of what ``BUILTIN_SKILLS`` declares — so a builtin skill never occupies
system-prompt budget by default, while staying reachable: the model finds it
by calling ``skill_list``, then reads its ``SKILL.md`` body with the ordinary
``file`` read op (which resolves builtin paths via
``reyn.builtin.docs.read_builtin_body_bytes``, #2913/#2914). ``enabled``
stays whatever the entry declares (default ``True``).

This force previously stamped ``auto_invoke=False``, and this docstring
claimed the result was "discoverable ... but requires an explicit
invocation". **Both halves were false, and #2971 is the correction.** The
L1 menu was the only surface that named a skill, and ``auto_invoke=False``
removed the entry from exactly that surface — so a builtin skill was not
discoverable by anyone, and no path existed by which anything could
explicitly invoke it. A3 was reasoning about a distinction the system did
not implement: nothing has ever auto-invoked a skill, so "auto-invoke vs
explicit invocation" named no real difference, and the intended middle state
("exists, but do not advertise it") was simply not expressible. The
``visibility`` enum makes it expressible, and ``skill_list`` supplies the
discovery surface that makes "inert" mean quiet rather than unreachable.

Pipelines and presentations need no equivalent force: both are invoke-by-name
(``PipelineRegistry`` / ``PresentationRegistry`` never self-trigger), so
registering one is already inert until something names it (Addendum A3) —
forcing a ``visibility``-shaped field on them would invent state that does
not exist in their schemas. Note what this asymmetry originally cost: a
pipeline's inertness came with ``run_pipeline`` as a by-name surface, and A3
copied the "ship inert" posture to skills without copying that surface.

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

from reyn.data.skills.registry import VISIBILITY_ON_DEMAND

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
#   BUILTIN_SKILLS = {"<name>": {"description": "...", "path": "...", "enabled": True, "visibility": "on_demand"}}
#   BUILTIN_PIPELINES = {"<key>": {"path": "...", "description": "...", "enabled": True}}
#   BUILTIN_PRESENTATIONS = {"<name>": {"blueprint": {...}, "enabled": True}}
#
# ``path`` is computed ABSOLUTE, relative to THIS module's own file location
# (not project-root-relative) — the builtin content ships inside the
# installed package (the ``builtin/**/*`` package-data glob, F3a Addendum A1),
# physically outside any given user's project_root, so a project-relative
# path would not resolve. (F3b left reading such a path at L2 subject to the
# standard out-of-project-root file-read permission gate — which hard-fails
# non-interactively, there being no operator to approve it. #2913/#2914 closed
# that: `reyn.builtin.docs.read_builtin_body_bytes` short-circuits the gate for
# builtin-provenance body reads only, scoped least-privilege to the `skills` /
# `pipelines` top-level dirs. #2971 depends on this — it is what lets a model
# act on a `skill_list` result with an ordinary `file` read.)
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
        # visibility is force-stamped "on_demand" for every builtin skill by
        # _stamp_builtin_entry (A3) regardless of what's declared here —
        # kept explicit for readability, not because it changes anything.
        "visibility": VISIBILITY_ON_DEMAND,
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
        "visibility": VISIBILITY_ON_DEMAND,
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


def _stamp_builtin_entry(entry: "dict[str, Any]", *, force_visibility_on_demand: bool) -> "dict[str, Any]":
    """Return *entry* with ``provenance="builtin"`` stamped (A9 seam).

    ``force_visibility_on_demand`` is set for skills only (A3 inert-ship — see
    module docstring); pipelines/presentations have no ``visibility`` field to
    force (their inertness is structural, not flag-based).
    """
    stamped = {**entry, "provenance": "builtin"}
    if force_visibility_on_demand:
        stamped["visibility"] = VISIBILITY_ON_DEMAND
    return stamped


def _stamp_builtin_entries(
    raw_entries: "dict[str, dict[str, Any]]", *, force_visibility_on_demand: bool = False,
) -> "dict[str, dict[str, Any]]":
    """Stamp ``provenance="builtin"`` onto every entry in *raw_entries*."""
    return {
        name: _stamp_builtin_entry(entry, force_visibility_on_demand=force_visibility_on_demand)
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
    additionally have ``visibility`` forced to ``"on_demand"`` (A3).

    F3a ships ``BUILTIN_SKILLS``/``BUILTIN_PIPELINES``/``BUILTIN_PRESENTATIONS``
    empty, so this returns three empty ``entries`` dicts — a no-op merge.
    """
    return {
        "skills": {"entries": _stamp_builtin_entries(BUILTIN_SKILLS, force_visibility_on_demand=True)},
        "pipelines": {"entries": _stamp_builtin_entries(BUILTIN_PIPELINES)},
        "presentations": {"entries": _stamp_builtin_entries(BUILTIN_PRESENTATIONS)},
    }
